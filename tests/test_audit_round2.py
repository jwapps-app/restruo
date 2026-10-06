"""Second-audit findings A05–A08."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.instances import InstanceRecord, InstanceStore, destination
from app.main import app
from app.portainer import PortainerClient

BASIC = ("admin", "hunter2")


def _reset():
    for attr in ("config", "store"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    monkeypatch.setenv("RESTRUO_USERNAME", "admin")
    _reset()
    with TestClient(app) as c:
        yield c


class StackHost:
    """A Portainer whose stack 1 is `portainer`; the listing can be made to fail,
    and the container's tag can be made to have moved (bare sha256 image)."""
    instance = type("I", (), {"name": "p"})()

    def __init__(self, *, listing_fails=False, moved_tag=False):
        self.listing_fails, self.moved_tag = listing_fails, moved_tag
        self.stopped, self.updated = [], []

    async def get_stack(self, sid):
        return {"Id": sid, "Name": "portainer", "EndpointId": 1, "Env": [], "Type": 2}

    async def list_containers(self, endpoint_id):
        if self.listing_fails:
            raise RuntimeError("proxy failure")
        image = "sha256:" + "c" * 64 if self.moved_tag else "portainer/portainer-ce:lts"
        return [{"Id": "a" * 12, "Names": ["/portainer"], "Image": image, "ImageID": "sha256:" + "c" * 64,
                 "State": "running", "Labels": {"com.docker.compose.project": "portainer"}}]

    async def get_image_info(self, endpoint_id, image):
        return {"Id": "sha256:" + "c" * 64, "RepoTags": [], "RepoDigests": []}

    async def get_container_info(self, endpoint_id, cid):
        return {"Config": {"Image": "portainer/portainer-ce:lts"}}

    async def set_stack_state(self, sid, endpoint_id, running):
        self.stopped.append(sid)

    async def update_stack(self, stack):
        self.updated.append(stack["Id"])

    async def aclose(self):
        pass


# --- A05 ------------------------------------------------------------------

def test_stop_guard_fails_closed_when_it_cannot_look(client):
    host = StackHost(listing_fails=True)
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/stacks/1/stop", auth=BASIC)
    assert r.status_code == 502 and "refusing" in r.json()["detail"]
    assert host.stopped == []


def test_update_guard_fails_closed_when_it_cannot_look(client):
    host = StackHost(listing_fails=True)
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/stacks/1/update", auth=BASIC)
    assert r.status_code == 502 and host.updated == []


def test_guards_see_through_a_moved_tag(client):
    """The container reports a bare image id; the guard must still recognise
    Portainer behind it."""
    host = StackHost(moved_tag=True)
    app.state.manager._clients[7] = host
    assert client.post("/api/instances/7/stacks/1/stop", auth=BASIC).status_code == 400
    assert client.post("/api/instances/7/stacks/1/update", auth=BASIC).status_code == 400
    assert host.stopped == [] and host.updated == []


# --- A06 ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_identifiers_never_leave_the_client():
    def handler(request):
        p = request.url.path
        if p == "/api/stacks":
            return httpx.Response(200, json=[
                {"Id": 1, "Name": "ok", "EndpointId": 2},
                {"Id": '1"><img src=x onerror=alert(1)>', "Name": "evil", "EndpointId": 2},
                {"Id": 3, "Name": "no-endpoint"},
            ])
        if p == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 2}, {"Id": "2 onload=x"}, "junk"])
        if p.endswith("/containers/json"):
            return httpx.Response(200, json=[{"Id": "3c8646c2b71b"}, {"Id": "<svg/onload=1>"}, {"Id": 5}])
        return httpx.Response(404)

    c = PortainerClient(InstanceRecord(id=1, name="p", base_url="https://p.test", auth_type="api_key", api_key="k"),
                        transport=httpx.MockTransport(handler))
    assert [s["Id"] for s in await c.list_stacks()] == [1]
    assert [e["Id"] for e in await c.list_endpoints()] == [2]
    assert [x["Id"] for x in await c.list_containers(2)] == ["3c8646c2b71b"]


def test_every_id_in_the_page_is_escaped():
    html = open("web/index.html").read()
    for raw in ('data-sid="${s.id}"', 'data-iid="${iid}"', 'data-eid="${c.endpointId ?? ""}"', '-${s.id}"'):
        assert raw not in html, raw


# --- A07 ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_refuses_to_move_a_stored_secret_to_a_new_address(tmp_path):
    store = InstanceStore(tmp_path / "i.json")
    rec = await store.add({"name": "n", "base_url": "https://orig.example:9443", "api_key": "ptr_SYN"})
    with pytest.raises(ValueError, match="address changed"):
        await store.update(rec.id, {"name": "n", "base_url": "https://other.example:9443", "api_key": ""})
    with pytest.raises(ValueError, match="address changed"):
        await store.update(rec.id, {"name": "n", "base_url": "http://orig.example:9443", "api_key": ""})  # downgrade
    assert store.get(rec.id).base_url == "https://orig.example:9443"
    # Same destination, blank key: the stored key stays, as before.
    updated = await store.update(rec.id, {"name": "renamed", "base_url": "https://orig.example:9443/", "api_key": ""})
    assert updated.name == "renamed" and updated.api_key == "ptr_SYN"
    # New destination with a fresh key is fine.
    updated = await store.update(rec.id, {"name": "n", "base_url": "https://other.example:9443", "api_key": "ptr_NEW"})
    assert updated.api_key == "ptr_NEW"


def test_edit_endpoint_enforces_it(client):
    iid = client.post("/api/instances", auth=BASIC,
                      json={"name": "n", "baseUrl": "https://orig.example", "apiKey": "ptr_SYN"}).json()["id"]
    r = client.put(f"/api/instances/{iid}", auth=BASIC, json={"name": "n", "baseUrl": "https://other.example", "apiKey": ""})
    assert r.status_code == 422 and "address changed" in r.json()["detail"]
    assert app.state.store.get(iid).base_url == "https://orig.example"


@pytest.mark.parametrize("a,b,same", [
    ("https://h:9443", "https://h:9443/", True),
    ("https://H:9443", "https://h:9443", True),
    ("https://h:9443", "http://h:9443", False),
    ("https://h:9443", "https://h:9444", False),
])
def test_destination_identity(a, b, same):
    assert (destination(a) == destination(b)) is same


# --- A08 ------------------------------------------------------------------

def test_new_instances_verify_tls_by_default():
    html = open("web/index.html").read()
    assert 'id="f-verify" checked' in html
    assert 'rec ? rec.verifyTls : true' in html


# --- A10: an untrusted realm is never fetched ---------------------------------

@pytest.mark.asyncio
async def test_internal_realm_is_never_requested():
    from app.registry import RegistryClient, RegistryError, parse_image_ref
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if "/v2/" in request.url.path:
            return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="http://127.0.0.1:9000/internal",service="x"'})
        return httpx.Response(200, json={"token": "t"})

    r = RegistryClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RegistryError, match="not an https URL"):
        await r.get_remote_digest(parse_image_ref("evil.example/app:latest"))
    assert not any("127.0.0.1" in u for u in requested)


# --- A11 ------------------------------------------------------------------

def test_smtp_security_typo_is_refused(monkeypatch):
    from app.config import EmailConfig
    monkeypatch.setenv("RESTRUO_SMTP_SECURITY", "startls")
    with pytest.raises(ValueError, match="RESTRUO_SMTP_SECURITY"):
        EmailConfig()
    monkeypatch.setenv("RESTRUO_SMTP_SECURITY", " SSL ")
    assert EmailConfig().security == "ssl"


# --- A13: the operator's tag decides whether an image floats ---------------

@pytest.mark.asyncio
async def test_moved_tag_with_a_digest_left_behind_still_reads_as_latest():
    from app.portainer import resolve_image_name
    def handler(request):
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"Id": "sha256:old", "RepoTags": [], "RepoDigests": ["nginx@sha256:" + "a" * 64]})
        return httpx.Response(200, json={"Config": {"Image": "nginx:latest"}})
    c = PortainerClient(InstanceRecord(id=1, name="p", base_url="https://p", auth_type="api_key", api_key="k"),
                        transport=httpx.MockTransport(handler))
    assert await resolve_image_name(c, 1, {"Id": "c" * 12, "Image": "sha256:old", "ImageID": "sha256:old"}) == "nginx:latest"


# --- A14: one current replica cannot vouch for an old one ------------------

@pytest.mark.asyncio
async def test_an_old_replica_beside_a_new_one_is_an_update():
    from app.registry import RegistryClient
    from app.updates import UpdateChecker
    NEW, OLD = "sha256:" + "n" * 64, "sha256:" + "o" * 64

    def portainer(request):
        p = request.url.path
        if p == "/api/stacks":
            return httpx.Response(200, json=[{"Id": 1, "Name": "web", "EndpointId": 1, "Env": []}])
        if p == "/api/stacks/1/file":
            return httpx.Response(200, json={"StackFileContent": "services:\n  web:\n    image: acme/web:latest\n"})
        if p == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 1, "Name": "x"}])
        if p.endswith("/containers/json"):
            return httpx.Response(200, json=[
                {"Id": "a" * 12, "Image": "acme/web:latest", "ImageID": "sha256:newimg", "State": "running", "Labels": {"com.docker.compose.project": "web"}},
                {"Id": "b" * 12, "Image": "acme/web:latest", "ImageID": "sha256:oldimg", "State": "running", "Labels": {"com.docker.compose.project": "web"}},
            ])
        if "/images/sha256:newimg/" in p:
            return httpx.Response(200, json={"Id": "sha256:newimg", "RepoDigests": [f"acme/web@{NEW}"]})
        if "/images/sha256:oldimg/" in p:
            return httpx.Response(200, json={"Id": "sha256:oldimg", "RepoDigests": [f"acme/web@{OLD}"]})
        return httpx.Response(404, json={"message": "nope"})

    registry = RegistryClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"docker-content-digest": NEW})))
    c = PortainerClient(InstanceRecord(id=1, name="p", base_url="https://p", auth_type="api_key", api_key="k"),
                        transport=httpx.MockTransport(portainer))
    snap = await UpdateChecker(lambda: [(1, c)], registry, interval_hours=6).check_all()
    image = snap["instances"][0]["stacks"][0]["images"][0]
    assert image["status"] == "update-available", image
    assert "o" * 12 in image["detail"] and "n" * 12 not in image["detail"].split("registry")[0]


# --- A15: a profiled image also run by an active service is checked -------

def test_profiled_image_shared_with_an_active_service_is_not_skipped():
    from app.portainer import profiled_images
    compose = "services:\n  a:\n    image: nginx:latest\n  b:\n    profiles: [x]\n    image: nginx:latest\n"
    assert "nginx:latest" in profiled_images(compose)  # still reported as profiled…


@pytest.mark.asyncio
async def test_checker_checks_a_profiled_image_when_a_container_runs_it():
    from app.registry import RegistryClient
    from app.updates import UpdateChecker
    D = "sha256:" + "d" * 64
    compose = "services:\n  a:\n    image: nginx:latest\n  b:\n    profiles: [x]\n    image: nginx:latest\n"

    def portainer(request):
        p = request.url.path
        if p == "/api/stacks":
            return httpx.Response(200, json=[{"Id": 1, "Name": "s", "EndpointId": 1, "Env": []}])
        if p == "/api/stacks/1/file":
            return httpx.Response(200, json={"StackFileContent": compose})
        if p == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 1, "Name": "x"}])
        if p.endswith("/containers/json"):
            return httpx.Response(200, json=[{"Id": "a" * 12, "Image": "nginx:latest", "ImageID": "sha256:i", "State": "running", "Labels": {"com.docker.compose.project": "s"}}])
        if "/images/" in p:
            return httpx.Response(200, json={"Id": "sha256:i", "RepoDigests": [f"nginx@{D}"]})
        return httpx.Response(404, json={"message": "nope"})

    registry = RegistryClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"docker-content-digest": D})))
    c = PortainerClient(InstanceRecord(id=1, name="p", base_url="https://p", auth_type="api_key", api_key="k"),
                        transport=httpx.MockTransport(portainer))
    snap = await UpdateChecker(lambda: [(1, c)], registry, interval_hours=6).check_all()
    assert snap["instances"][0]["stacks"][0]["images"][0]["status"] == "up-to-date"


# --- A16: structural parsing and compose interpolation ----------------------

def test_flow_style_yaml_and_odd_sections_parse_structurally():
    from app.portainer import extract_images
    assert extract_images('services: {web: {image: "nginx:latest"}}') == ["nginx:latest"]
    assert extract_images("x-common:\n  image: not-a-service\nservices:\n  a:\n    image: real:1\n") == ["real:1"]
    # Unparseable YAML: the line regex still rescues what it can.
    assert extract_images("services:\n  a:\n    image: nginx:1\n  b: [bad") == ["nginx:1"]


def test_empty_declaration_falls_back_to_the_containers():
    from app.portainer import stack_images
    containers = [{"Image": "acme/web:latest"}]
    assert stack_images({"Env": []}, "", containers) == ["acme/web:latest"]


@pytest.mark.parametrize("value,env,expected", [
    ("nginx:${TAG:-latest}", {}, "nginx:latest"),
    ("nginx:${TAG:-latest}", {"TAG": ""}, "nginx:latest"),      # :- also covers empty
    ("nginx:${TAG-latest}", {"TAG": ""}, "nginx:"),             # - only covers unset
    ("nginx:${TAG-latest}", {}, "nginx:latest"),
    ("nginx:${TAG:?set it}", {}, None),                          # an error, not a default
    ("nginx:${TAG?set it}", {"TAG": ""}, "nginx:"),              # ? is satisfied by set-but-empty
    ("nginx:${TAG:?set it}", {"TAG": "1.27"}, "nginx:1.27"),
])
def test_interpolation_follows_compose(value, env, expected):
    from app.portainer import interpolate
    assert interpolate(value, env) == expected


# --- A17: a failed read is reported as such ---------------------------------

def test_stacks_endpoint_reports_an_environment_it_could_not_read(client):
    class Host:
        instance = type("I", (), {"name": "p"})()
        async def list_stacks(self):
            return [{"Id": 1, "Name": "s", "EndpointId": 2, "Type": 2, "Status": 1}]
        async def list_endpoints(self):
            return [{"Id": 2, "Name": "flaky"}]
        async def list_containers(self, endpoint_id):
            raise RuntimeError("agent gone")
        async def get_stack_file(self, sid):
            return "services:\n  a:\n    image: x:latest\n"
        async def aclose(self):
            pass
    iid = client.post("/api/instances", auth=BASIC,
                      json={"name": "p", "baseUrl": "https://p.test", "apiKey": "k"}).json()["id"]
    app.state.manager._clients[iid] = Host()
    r = client.get("/api/stacks", auth=BASIC)
    inst = next(i for i in r.json() if i["instance"]["id"] == iid)
    assert inst["reachable"] is True
    assert "flaky" in inst["environmentErrors"]
    assert inst["stacks"][0]["unchecked"] is True
    assert inst["stacks"][0]["downNames"] == []


# --- A18: a project named like a stack elsewhere is still a container here --

def test_stack_names_are_scoped_to_their_environment():
    from app.portainer import stack_names_by_endpoint, standalone_containers
    stacks = [{"Name": "monitoring", "EndpointId": 1}]
    names = stack_names_by_endpoint(stacks)
    external = [{"Id": "e" * 12, "Labels": {"com.docker.compose.project": "monitoring"}}]
    assert standalone_containers(external, names.get(2, set())) == external, "on env 2 it is not Portainer's"
    assert standalone_containers(external, names.get(1, set())) == []


# --- A20 -------------------------------------------------------------------

def test_auto_refresh_also_reloads_update_results():
    html = open("web/index.html").read()
    body = html[html.index("async function refresh()"):html.index("function ago(")]
    assert "loadUpdates()" in body
