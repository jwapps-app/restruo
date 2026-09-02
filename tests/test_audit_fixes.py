"""Regression tests for the server-side audit.

Each test names the hole it closes. Several reproduce the exact request that
was confirmed against the live app before the fix.
"""
import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import LoginLimiter, SessionManager
from app.instances import ClientManager, InstanceRecord, InstanceStore
from app.main import app
from app.portainer import PortainerClient
from app.registry import _realm_trusted
from app.updates import UpdateChecker

CSRF = {"X-Restruo": "1"}
BASIC = ("admin", "hunter2")


def _reset_app_state():
    for attr in ("config", "store"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    monkeypatch.setenv("RESTRUO_USERNAME", "admin")
    _reset_app_state()
    with TestClient(app) as test_client:
        yield test_client


def _login(client):
    r = client.post("/api/login", json={"username": "admin", "password": "hunter2"})
    assert r.status_code == 200
    return r


# --- #1 standalone-container updates must refuse an agent ------------------

class AgentHost:
    """A Portainer whose environment 11 runs a portainer/agent container."""
    instance = type("I", (), {"name": "Proxmox"})()
    recreated: list = []

    async def list_containers(self, endpoint_id):
        return [{"Id": "3c8646c2b71b", "Names": ["/portainer_agent"],
                 "Image": "portainer/agent:latest", "ImageID": "sha256:x",
                 "State": "running", "Labels": {}}]

    async def recreate_container(self, endpoint_id, cid):
        self.recreated.append(cid)

    async def aclose(self):
        pass


def test_container_update_refuses_a_portainer_agent(client):
    host = AgentHost()
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/containers/3c8646c2b71b/update?endpointId=11", auth=BASIC)
    assert r.status_code == 400
    assert "agent" in r.json()["detail"].lower()
    assert host.recreated == [], "the recreate must never reach Portainer"


# --- #2 CSRF: cookie sessions need the header on anything that changes -----

def test_cookie_session_change_without_header_is_refused(client):
    _login(client)
    # The request that was confirmed to succeed before the fix.
    r = client.post("/api/check-updates", headers={"Origin": "http://192.168.1.11:9443"})
    assert r.status_code == 403
    assert "X-Restruo" in r.json()["detail"]


def test_cookie_session_with_header_and_reads_are_fine(client):
    _login(client)
    assert client.get("/api/instances").status_code == 200          # no header needed to read
    assert client.post("/api/check-updates", headers=CSRF).status_code == 200


def test_basic_auth_is_exempt_from_the_header(client):
    # curl and scripts never carry the cookie, so they can't be ridden.
    assert client.post("/api/check-updates", auth=BASIC).status_code == 200


def test_logout_needs_the_header_too(client):
    _login(client)
    assert client.post("/api/logout").status_code == 403
    assert client.post("/api/logout", headers=CSRF).status_code == 200


# --- #3 replaying a request only when it never arrived ----------------------

RECORD = InstanceRecord(id=1, name="p", base_url="https://p.test:9443",
                        auth_type="api_key", api_key="k")


@pytest.mark.asyncio
async def test_connect_error_is_retried_once():
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=[])

    c = PortainerClient(RECORD, transport=httpx.MockTransport(handler))
    assert await c.list_endpoints() == []
    assert calls == ["GET", "GET"]


@pytest.mark.asyncio
async def test_read_timeout_is_not_replayed():
    """A PUT that timed out may already have deployed. Sending it again is a
    second deploy."""
    calls = []

    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("slow", request=request)

    c = PortainerClient(RECORD, transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout):
        await c.redeploy_compose(1, 1, "services: {}", [])
    assert calls == ["PUT"], "exactly one attempt"


# --- #4 a pathological cookie is a 401, not a crash ------------------------

def test_oversized_cookie_is_rejected_not_a_500(client):
    client.cookies.set("restruo_session", "9" * 5000 + ".abc")
    assert client.get("/api/instances").status_code == 401


# --- #5 sessions: password change signs everyone out; no shared tokens -----

def test_password_change_invalidates_sessions(tmp_path):
    before = SessionManager(tmp_path / "secret", "old-password")
    token = before.issue()
    assert before.verify(token)
    after = SessionManager(tmp_path / "secret", "new-password")
    assert not after.verify(token)
    # Same password again → same key: sessions survive a plain restart.
    assert SessionManager(tmp_path / "secret", "old-password").verify(token)


def test_two_logins_never_share_a_token(tmp_path):
    m = SessionManager(tmp_path / "secret", "pw")
    assert m.issue() != m.issue()


@pytest.mark.parametrize("token", ["", "abc", "1.2", "1.2.3.4", "x" * 200,
                                   "99999999999999.nonce.sig"])
def test_malformed_tokens_are_simply_invalid(tmp_path, token):
    m = SessionManager(tmp_path / "secret", "pw")
    assert m.verify(token) is False


# --- #6 brute force: a budget per address, and failures are logged ---------

def test_limiter_blocks_after_budget_and_resets_on_success():
    lim = LoginLimiter(max_failures=3, window_seconds=60)
    for _ in range(3):
        assert not lim.blocked("10.0.0.1")
        lim.record_failure("10.0.0.1")
    assert lim.blocked("10.0.0.1")
    assert not lim.blocked("10.0.0.2"), "budgets are per address"
    lim.reset("10.0.0.1")
    assert not lim.blocked("10.0.0.1")


def test_limiter_window_expires(monkeypatch):
    lim = LoginLimiter(max_failures=1, window_seconds=10)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    lim.record_failure("a")
    assert lim.blocked("a")
    monkeypatch.setattr(time, "time", lambda: now + 11)
    assert not lim.blocked("a")


def test_login_endpoint_rate_limits(client, caplog):
    app.state.limiter = LoginLimiter(max_failures=3, window_seconds=60)
    for _ in range(3):
        assert client.post("/api/login", json={"username": "admin", "password": "no"}).status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "no"}).status_code == 429
    # Even the right password is refused while blocked — that's the point.
    assert client.post("/api/login", json={"username": "admin", "password": "hunter2"}).status_code == 429
    assert "Failed login" in caplog.text


def test_basic_auth_path_is_limited_too(client):
    app.state.limiter = LoginLimiter(max_failures=2, window_seconds=60)
    for _ in range(2):
        assert client.get("/api/instances", auth=("admin", "no")).status_code == 401
    assert client.get("/api/instances", auth=("admin", "no")).status_code == 429


# --- #7 nothing personal before login -------------------------------------

def test_ui_config_hides_email_until_authenticated(client):
    public = client.get("/api/ui-config").json()
    assert "email" not in public and "refreshSeconds" not in public
    assert set(public) == {"title", "version", "authEnabled"}
    private = client.get("/api/ui-config", auth=BASIC).json()
    assert "email" in private and "refreshSeconds" in private


# --- #8 validation errors don't quote the secret ---------------------------

def test_validation_errors_never_echo_the_secret(client):
    # credentials without a password fails validation; api_key must not appear.
    r = client.post("/api/instances", auth=BASIC, json={
        "name": "x", "baseUrl": "http://h", "authType": "credentials",
        "username": "u", "apiKey": "ptr_SECRET_TOKEN_123",
    })
    assert r.status_code == 422
    assert "ptr_SECRET_TOKEN_123" not in r.text
    with pytest.raises(ValueError) as caught:
        InstanceRecord.model_validate({"id": 0, "name": "x", "base_url": "http://h",
                                       "auth_type": "credentials", "username": "u",
                                       "api_key": "ptr_SECRET_TOKEN_123"})
    assert "ptr_SECRET_TOKEN_123" not in str(caught.value)


# --- #9 headers that stop framing and sniffing ------------------------------

def test_security_headers_on_every_response(client):
    for path in ("/", "/api/ui-config"):
        h = client.get(path).headers
        assert h["x-frame-options"] == "DENY", path
        assert "frame-ancestors 'none'" in h["content-security-policy"], path
        assert h["x-content-type-options"] == "nosniff", path


# --- #10 a stored secret only goes to the address it was saved for ----------

def test_stored_secret_is_not_sent_to_a_new_address(client):
    r = client.post("/api/instances", auth=BASIC, json={
        "name": "nas", "baseUrl": "http://192.168.1.19:9000", "apiKey": "ptr_real"})
    iid = r.json()["id"]
    r = client.post(f"/api/instances/test?id={iid}", auth=BASIC, json={
        "name": "nas", "baseUrl": "http://attacker.example:9000", "apiKey": ""})
    assert r.json()["ok"] is False
    assert "address changed" in r.json()["error"].lower()


# --- #12 the Secure flag follows the scheme --------------------------------

def test_secure_flag_only_over_https(client):
    plain = client.post("/api/login", json={"username": "admin", "password": "hunter2"})
    assert "secure" not in plain.headers["set-cookie"].lower()
    tls = client.post("/api/login", json={"username": "admin", "password": "hunter2"},
                      headers={"X-Forwarded-Proto": "https"})
    assert "secure" in tls.headers["set-cookie"].lower()


# --- #13 registry logins only go to the registry's own HTTPS realm ---------

@pytest.mark.parametrize("realm,registry,ok", [
    ("https://auth.docker.io/token", "docker.io", True),
    ("https://ghcr.io/token", "ghcr.io", True),
    ("http://auth.docker.io/token", "docker.io", False),      # plain http
    ("https://evil.example/token", "ghcr.io", False),         # other domain
    ("https://registry.lan:5000/auth", "registry.lan:5000", True),
    ("https://10.0.0.5/auth", "10.0.0.5:5000", True),
])
def test_realm_trust(realm, registry, ok):
    assert _realm_trusted(realm, registry) is ok


# --- #14/#19 an edit rebuilds only the instance that changed ----------------

@pytest.mark.asyncio
async def test_refresh_keeps_clients_whose_record_is_unchanged(tmp_path):
    store = InstanceStore(tmp_path / "instances.json")
    a = await store.add({"name": "a", "base_url": "http://a", "api_key": "k"})
    b = await store.add({"name": "b", "base_url": "http://b", "api_key": "k"})
    manager = ClientManager(store)
    await manager.refresh()
    client_a, client_b = manager.get(a.id), manager.get(b.id)
    await store.update(b.id, {"name": "b2"})
    await manager.refresh()
    assert manager.get(a.id) is client_a, "untouched instance keeps its session"
    assert manager.get(b.id) is not client_b, "edited instance gets a fresh client"
    await manager.aclose()


# --- #16 a failed poll is not "every container vanished" -------------------

@pytest.mark.asyncio
async def test_transient_listing_error_does_not_fake_a_redeploy(monkeypatch):
    monkeypatch.setattr(main, "DEPLOY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(main, "DEPLOY_TIMEOUT_SECONDS", 0.5)
    same = [{"Id": "c1", "State": "running", "Status": "Up",
             "Labels": {"com.docker.compose.project": "s"}}]

    class Flaky:
        calls = 0

        async def list_containers(self, endpoint_id):
            Flaky.calls += 1
            if Flaky.calls % 2 == 0:
                raise RuntimeError("proxy hiccup")
            return same

    stack = {"Id": 1, "Name": "s", "EndpointId": 1}
    before = await main._stack_fingerprint(Flaky(), stack)
    message = await main._await_deploy(Flaky(), stack, before)
    assert "nothing changed" in message
    assert "Redeployed" not in message


# --- #17 long deploys become a job the page polls --------------------------

def test_quick_update_answers_inline_and_slow_one_becomes_a_job(client, monkeypatch):
    monkeypatch.setattr(main, "JOB_SYNC_WAIT_SECONDS", 0.05)

    class Slow:
        instance = type("I", (), {"name": "p"})()
        gate = asyncio.Event()

        async def get_stack(self, sid):
            return {"Id": sid, "Name": "s", "EndpointId": 1, "Env": []}

        async def list_containers(self, endpoint_id):
            return []

        async def update_stack(self, stack):
            return {}

        async def aclose(self):
            pass

    app.state.manager._clients[9] = Slow()
    # _update_one would poll for the deploy; make it return instantly and
    # then, for the second call, slowly.
    outcomes = iter(["fast", "slow"])

    async def fake_update_one(c, stack):
        if next(outcomes) == "slow":
            await asyncio.sleep(0.3)
        return {"ok": True, "stack": "s", "durationMs": 1, "message": "Redeployed."}

    monkeypatch.setattr(main, "_update_one", fake_update_one)

    quick = client.post("/api/instances/9/stacks/1/update", auth=BASIC)
    assert quick.status_code == 200 and quick.json()["ok"] is True

    slow = client.post("/api/instances/9/stacks/1/update", auth=BASIC)
    assert slow.status_code == 202
    job_id = slow.json()["jobId"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}", auth=BASIC).json()
        if job["done"]:
            break
        time.sleep(0.02)
    assert job["done"] and job["result"]["ok"] is True
    assert client.get("/api/jobs/nope", auth=BASIC).status_code == 404


# --- #18 what was announced survives a restart ------------------------------

def test_notified_state_persists(tmp_path):
    path = tmp_path / "notified.json"
    first = UpdateChecker(lambda: [], registry=None, interval_hours=6, state_path=path)
    first._notified = {(5, 12, "img:latest"), (5, "container", 11, "abc", "x:latest")}
    first._save_notified()
    second = UpdateChecker(lambda: [], registry=None, interval_hours=6, state_path=path)
    assert second._notified == first._notified
    assert json.loads(path.read_text())  # plain JSON on disk


def test_corrupt_notified_state_is_ignored(tmp_path):
    path = tmp_path / "notified.json"
    path.write_text("{not json")
    checker = UpdateChecker(lambda: [], registry=None, interval_hours=6, state_path=path)
    assert checker._notified == set()
