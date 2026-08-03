"""Tests for the instance store and username/password (JWT) authentication."""

import json

import httpx
import pytest

from app.instances import InstanceRecord, InstanceStore
from app.portainer import PortainerClient, PortainerError

CRED_INSTANCE = InstanceRecord(
    id=1, name="Cred", base_url="https://portainer.test:9443", verify_tls=True,
    auth_type="credentials", username="admin", password="hunter2",
)


# --- InstanceStore ----------------------------------------------------------

async def test_store_crud_and_persistence(tmp_path):
    path = tmp_path / "instances.json"
    store = InstanceStore(path)
    assert store.list() == []

    record = await store.add({
        "name": "A", "base_url": "https://a.test:9443/",
        "auth_type": "api_key", "api_key": "ptr_a",
    })
    assert record.id == 1
    assert record.base_url == "https://a.test:9443"  # trailing slash stripped

    # Reload from disk — persisted.
    reloaded = InstanceStore(path)
    assert reloaded.get(1).name == "A"

    # Update without secret keeps the stored secret.
    updated = await store.update(1, {
        "name": "A2", "base_url": "https://a.test:9443",
        "auth_type": "api_key", "api_key": "",
    })
    assert updated.name == "A2"
    assert updated.api_key == "ptr_a"

    # Switching auth type requires the new secret.
    switched = await store.update(1, {
        "name": "A2", "base_url": "https://a.test:9443",
        "auth_type": "credentials", "username": "admin", "password": "pw",
    })
    assert switched.auth_type == "credentials"

    assert await store.delete(1) is True
    assert await store.delete(1) is False
    assert store.list() == []


async def test_store_move_reorders_and_persists(tmp_path):
    path = tmp_path / "instances.json"
    store = InstanceStore(path)
    for name in ("A", "B", "C"):
        await store.add({
            "name": name, "base_url": f"https://{name}.test",
            "auth_type": "api_key", "api_key": "k",
        })
    assert [r.name for r in store.list()] == ["A", "B", "C"]

    assert await store.move(3, "up") is True
    assert [r.name for r in store.list()] == ["A", "C", "B"]
    assert await store.move(1, "down") is True
    assert [r.name for r in store.list()] == ["C", "A", "B"]

    # Ends refuse to move past the edge, and unknown ids are rejected.
    assert await store.move(3, "up") is False   # C is already first
    assert await store.move(2, "down") is False  # B is already last
    assert await store.move(99, "up") is False

    # Order survives a restart — it's the display order.
    assert [r.name for r in InstanceStore(path).list()] == ["C", "A", "B"]


async def test_store_file_is_owner_only(tmp_path):
    path = tmp_path / "instances.json"
    store = InstanceStore(path)
    await store.add({
        "name": "A", "base_url": "https://a.test", "auth_type": "api_key", "api_key": "k",
    })
    assert (path.stat().st_mode & 0o777) == 0o600
    # Pre-existing world-readable files get repaired on load.
    path.chmod(0o644)
    InstanceStore(path)
    assert (path.stat().st_mode & 0o777) == 0o600


async def test_store_validation_rejects_missing_secrets(tmp_path):
    store = InstanceStore(tmp_path / "instances.json")
    with pytest.raises(ValueError):
        await store.add({"name": "X", "base_url": "https://x.test", "auth_type": "api_key"})
    with pytest.raises(ValueError):
        await store.add({
            "name": "X", "base_url": "https://x.test",
            "auth_type": "credentials", "username": "admin",
        })


async def test_store_seed(tmp_path):
    path = tmp_path / "instances.json"
    store = InstanceStore(path)
    await store.seed([
        {"name": "A", "base_url": "https://a.test", "auth_type": "api_key", "api_key": "k1"},
        {"name": "B", "base_url": "https://b.test", "auth_type": "api_key", "api_key": "k2"},
    ])
    assert [r.id for r in store.list()] == [1, 2]
    assert json.loads(path.read_text())[1]["name"] == "B"


def test_public_shape_has_no_secrets():
    public = CRED_INSTANCE.public()
    assert "api_key" not in str(public)
    assert "hunter2" not in str(public)
    assert public["authType"] == "credentials"
    assert public["username"] == "admin"


# --- credentials (JWT) auth flow -------------------------------------------

def jwt_portainer_transport(state: dict) -> httpx.MockTransport:
    """Mock Portainer requiring a JWT; `state` records logins and lets a test
    invalidate the current token to simulate expiry."""
    state.setdefault("logins", 0)
    state.setdefault("valid_jwt", None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            body = json.loads(request.content)
            if body != {"Username": "admin", "Password": "hunter2"}:
                return httpx.Response(422, json={"message": "invalid credentials"})
            state["logins"] += 1
            state["valid_jwt"] = f"jwt-{state['logins']}"
            return httpx.Response(200, json={"jwt": state["valid_jwt"]})
        if request.headers.get("Authorization") != f"Bearer {state['valid_jwt']}":
            return httpx.Response(401, json={"message": "unauthorized"})
        if request.url.path == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 2}])
        raise AssertionError(f"unexpected request: {request.url.path}")

    return httpx.MockTransport(handler)


async def test_credentials_login_and_reuse():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=jwt_portainer_transport(state))
    assert await client.list_endpoints() == [{"Id": 2}]
    assert await client.list_endpoints() == [{"Id": 2}]
    assert state["logins"] == 1  # session JWT reused, not re-fetched per call
    await client.aclose()


async def test_credentials_relogin_on_expiry():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=jwt_portainer_transport(state))
    await client.list_endpoints()
    state["valid_jwt"] = "expired"  # simulate Portainer expiring the session
    assert await client.list_endpoints() == [{"Id": 2}]
    assert state["logins"] == 2
    await client.aclose()


async def test_credentials_bad_password_surfaces_error():
    state = {}
    bad = CRED_INSTANCE.model_copy(update={"password": "wrong"})
    client = PortainerClient(bad, transport=jwt_portainer_transport(state))
    with pytest.raises(PortainerError) as excinfo:
        await client.list_endpoints()
    assert "invalid credentials" in excinfo.value.message
    await client.aclose()


# --- CSRF handling (Portainer 2.20.2+ session-auth requirement) --------------

GIT_STACK = {
    "Id": 5, "Name": "s", "EndpointId": 2, "Type": 2,
    "GitConfig": {"URL": "https://github.com/x/y"}, "Env": [],
}


def csrf_portainer_transport(state: dict) -> httpx.MockTransport:
    """Mock of real Portainer 2.39 CSRF behaviour: the token is issued only on
    AUTHENTICATED /api responses. Unauthenticated requests and the SPA page
    return the header present but EMPTY, which is what broke fetching it from
    GET / — the client must harvest tokens from the API calls it already makes."""
    state.setdefault("csrf_fetches", 0)
    state.setdefault("valid_csrf", "csrf-1")
    state.setdefault("logins", 0)
    state.setdefault("valid_jwt", None)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth":
            state["logins"] += 1
            state["valid_jwt"] = f"jwt-{state['logins']}"
            return httpx.Response(200, json={"jwt": state["valid_jwt"]})
        if path == "/" and request.method == "GET":
            state["csrf_fetches"] += 1
            return httpx.Response(200, headers={"X-CSRF-Token": ""}, text="<html>")
        if request.headers.get("Authorization") != f"Bearer {state['valid_jwt']}":
            return httpx.Response(401, headers={"X-CSRF-Token": ""},
                                  json={"message": "unauthorized"})
        # Authenticated from here on: every /api response carries the token.
        issued = {"X-CSRF-Token": state["valid_csrf"]}
        if request.method in ("PUT", "POST", "DELETE"):
            if request.headers.get("X-CSRF-Token") != state["valid_csrf"]:
                return httpx.Response(403, headers=issued,
                                      text="Forbidden - CSRF token not found in request")
            # Over HTTPS Portainer also requires a same-origin referer.
            referer = request.headers.get("Referer", "")
            if not referer.startswith("https://portainer.test:9443"):
                return httpx.Response(403, headers=issued,
                                      text="Forbidden - referer not supplied")
        if path == "/api/endpoints":
            state["csrf_fetches"] += 1
            return httpx.Response(200, headers=issued, json=[{"Id": 2}])
        if path == "/api/stacks/5/git/redeploy":
            return httpx.Response(200, headers=issued, json={"Id": 5})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return httpx.MockTransport(handler)


async def test_credentials_mutating_request_sends_csrf_token():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=csrf_portainer_transport(state))
    await client.update_stack(GIT_STACK)
    fetches = state["csrf_fetches"]
    # Token is reused on the next mutating call, not re-fetched.
    await client.update_stack(GIT_STACK)
    assert state["csrf_fetches"] == fetches
    await client.aclose()


async def test_token_is_harvested_from_ordinary_api_calls():
    """A GET that the app makes anyway must supply the token, so a mutating
    call needs no separate handshake."""
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=csrf_portainer_transport(state))
    await client.list_endpoints()
    before = state["csrf_fetches"]
    await client.update_stack(GIT_STACK)
    assert state["csrf_fetches"] == before  # no extra token-fetch round trip
    await client.aclose()


async def test_credentials_refreshes_stale_csrf_token():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=csrf_portainer_transport(state))
    await client.update_stack(GIT_STACK)
    state["valid_csrf"] = "csrf-2"  # server rotated the token
    await client.update_stack(GIT_STACK)  # 403 → re-harvest → retry succeeds
    await client.aclose()


async def test_credentials_recover_from_portainer_restart():
    """A restart invalidates the JWT, CSRF token, and CSRF cookie at once —
    the client must rebuild the whole session inside one request."""
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=csrf_portainer_transport(state))
    await client.update_stack(GIT_STACK)

    state["valid_jwt"] = "gone-after-restart"
    state["valid_csrf"] = "csrf-after-restart"
    await client.update_stack(GIT_STACK)  # must self-heal, not wedge
    assert state["logins"] == 2
    await client.aclose()
