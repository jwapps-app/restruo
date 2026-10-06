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
