"""Two requests from the issue tracker.

#18 — a version tag (mariadb:10.11) is republished under the same name, so
"the tag did not change" does not mean "the image did not". Restruo filed
every such tag as pinned and never looked. Checking them is opt-in.

#19 — updating a stopped stack started it. Reproduced on a live Portainer:
stop a stack, press Update, and it came back running and marked active. The
same happened to a stopped standalone container, since Portainer's recreate
always starts what it creates.
"""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.instances import InstanceRecord
from app.main import app
from app.portainer import PortainerClient, registry_id_for
from app.registry import RegistryClient
from app.updates import UpdateChecker

BASIC = ("admin", "hunter2")
OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64
RECORD = InstanceRecord(id=1, name="p", base_url="https://p.test", auth_type="api_key", api_key="k")


# ---------------------------------------------------------------- #18

def lab(local_digest: str):
    """One stack running mariadb:10.11, nginx:latest and a digest-pinned image."""
    compose = ("services:\n  db:\n    image: mariadb:10.11\n  web:\n    image: nginx:latest\n"
               "  fixed:\n    image: redis@sha256:" + "c" * 64 + "\n")

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/stacks":
            return httpx.Response(200, json=[{"Id": 1, "Name": "s", "EndpointId": 1, "Env": []}])
        if p == "/api/stacks/1/file":
            return httpx.Response(200, json={"StackFileContent": compose})
        if p == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 1, "Name": "x"}])
        if p.endswith("/containers/json"):
            return httpx.Response(200, json=[
                {"Id": "a" * 12, "Image": "mariadb:10.11", "ImageID": "sha256:dbimg", "State": "running",
                 "Labels": {"com.docker.compose.project": "s"}},
                {"Id": "b" * 12, "Image": "nginx:latest", "ImageID": "sha256:webimg", "State": "running",
                 "Labels": {"com.docker.compose.project": "s"}},
            ])
        if "/images/sha256:dbimg/" in p:
            return httpx.Response(200, json={"Id": "sha256:dbimg", "RepoDigests": [f"mariadb@{local_digest}"]})
        if "/images/sha256:webimg/" in p:
            return httpx.Response(200, json={"Id": "sha256:webimg", "RepoDigests": [f"nginx@{NEW}"]})
        return httpx.Response(404, json={"message": "nope"})

    return PortainerClient(RECORD, transport=httpx.MockTransport(handler))


def registry_serving(digest: str, asked: list):
    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, headers={"docker-content-digest": digest})
    return RegistryClient(transport=httpx.MockTransport(handler))


async def check(local_digest, **kwargs):
    asked: list = []
    checker = UpdateChecker(lambda: [(1, lab(local_digest))], registry_serving(NEW, asked),
                            interval_hours=6, **kwargs)
    snap = await checker.check_all(notify=False)
    return {i["image"]: i for i in snap["instances"][0]["stacks"][0]["images"]}, asked


@pytest.mark.asyncio
async def test_version_tags_are_left_alone_unless_asked():
    images, asked = await check(OLD)
    db = images["mariadb:10.11"]
    assert db["status"] == "pinned"
    assert "RESTRUO_CHECK_VERSION_TAGS" in db["detail"], "says how to turn it on"
    assert not any("mariadb" in path for path in asked), "the registry was never asked"


@pytest.mark.asyncio
async def test_a_republished_version_tag_is_an_update_when_checking_is_on():
    images, asked = await check(OLD, check_version_tags=True)
    db = images["mariadb:10.11"]
    assert db["status"] == "update-available"
    assert db["rebuild"] is True
    assert "same tag" in db["detail"] and "a" * 12 in db["detail"] and "b" * 12 in db["detail"]
    assert any("/library/mariadb/manifests/10.11" in path for path in asked)


@pytest.mark.asyncio
async def test_a_current_version_tag_is_up_to_date_not_pinned():
    images, _ = await check(NEW, check_version_tags=True)
    assert images["mariadb:10.11"]["status"] == "up-to-date"


@pytest.mark.asyncio
async def test_a_digest_is_pinned_whatever_the_setting_and_a_channel_tag_is_no_rebuild():
    images, asked = await check(OLD, check_version_tags=True)
    fixed = images["redis@sha256:" + "c" * 64]
    assert fixed["status"] == "pinned" and "digest" in fixed["detail"]
    assert not any("redis" in path for path in asked)
    assert "rebuild" not in images["nginx:latest"]


def test_setting_comes_from_the_environment_and_defaults_off(monkeypatch):
    from app.config import UpdatesConfig
    monkeypatch.delenv("RESTRUO_CHECK_VERSION_TAGS", raising=False)
    assert UpdatesConfig().check_version_tags is False
    for value in ("true", "1", "YES", " on "):
        monkeypatch.setenv("RESTRUO_CHECK_VERSION_TAGS", value)
        assert UpdatesConfig().check_version_tags is True, value
    monkeypatch.setenv("RESTRUO_CHECK_VERSION_TAGS", "")
    assert UpdatesConfig().check_version_tags is False


def test_the_page_names_a_rebuild_and_explains_a_pin():
    html = open("web/index.html").read()
    assert 'img.rebuild ? "new build" : "new version"' in html
    assert 'title="${esc(img.detail || "Pinned to a specific version — not checked")}"' in html


# ---------------------------------------------------------------- #19

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


class StoppedStackHost:
    """A Portainer whose stack 1 is stopped (Status 2)."""
    instance = type("I", (), {"name": "p"})()
    COMPOSE = ("services:\n  api:\n    image: ghcr.io/acme/api:latest\n  db:\n    image: mariadb:10.11\n"
               "  extra:\n    profiles: [x]\n    image: acme/never:latest\n")

    def __init__(self, *, status=2, compose=None, env=None, fail=(), git=False):
        self.status, self.compose, self.env, self.fail, self.git = status, compose or self.COMPOSE, env or [], fail, git
        self.pulled, self.redeployed = [], []

    async def get_stack(self, sid):
        return {"Id": sid, "Name": "tools", "EndpointId": 4, "Status": self.status, "Type": 2,
                "Env": self.env, "GitConfig": {"URL": "x"} if self.git else None}

    async def list_containers(self, endpoint_id):
        return []  # a stopped stack has no containers

    async def get_stack_file(self, sid):
        return self.compose

    async def list_registries(self):
        return [{"Id": 7, "URL": "ghcr.io", "Authentication": True}]

    async def pull_image(self, endpoint_id, image, registry_id=None):
        if image in self.fail:
            raise RuntimeError("manifest unknown")
        self.pulled.append((endpoint_id, image, registry_id))

    async def update_stack(self, stack):
        self.redeployed.append(stack["Id"])

    async def aclose(self):
        pass


def test_updating_a_stopped_stack_pulls_and_never_redeploys(client):
    host = StoppedStackHost()
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/stacks/1/update", auth=BASIC)

    assert r.status_code == 200, r.text
    assert host.redeployed == [], "a redeploy would start it"
    assert sorted(host.pulled) == [(4, "ghcr.io/acme/api:latest", 7), (4, "mariadb:10.11", None)]
    message = r.json()["message"]
    assert "Pulled 2 images" in message and "stays stopped" in message


def test_a_running_stack_is_still_redeployed(client, monkeypatch):
    host = StoppedStackHost(status=1)
    app.state.manager._clients[7] = host

    async def fake_await(client_, stack, before):
        return {"ok": True, "message": "Redeployed 2 containers, all running."}
    monkeypatch.setattr(main, "_await_deploy", fake_await)
    r = client.post("/api/instances/7/stacks/1/update", auth=BASIC)
    assert r.status_code == 200 and host.redeployed == [1] and host.pulled == []


def test_a_failed_pull_is_not_called_an_update(client):
    host = StoppedStackHost(fail=("mariadb:10.11",))
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/stacks/1/update", auth=BASIC)
    assert r.status_code == 502
    message = r.json()["message"]
    assert "Pulled 1 of 2" in message and "mariadb:10.11" in message and "still stopped" in message


def test_variables_are_resolved_and_an_unresolvable_one_is_reported(client):
    compose = "services:\n  a:\n    image: acme/app:${TAG:-stable}\n  b:\n    image: ${PREFIX}/thing:latest\n"
    host = StoppedStackHost(compose=compose)
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/stacks/1/update", auth=BASIC)
    assert r.status_code == 502
    assert [p[1] for p in host.pulled] == ["acme/app:stable"]
    assert "${PREFIX}/thing:latest" in r.json()["message"]

    host = StoppedStackHost(compose=compose, env=[{"name": "PREFIX", "value": "ghcr.io/acme"}])
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/stacks/1/update", auth=BASIC)
    assert r.status_code == 200
    assert sorted(p[1] for p in host.pulled) == ["acme/app:stable", "ghcr.io/acme/thing:latest"]


def test_a_stopped_git_stack_says_what_was_not_done(client):
    host = StoppedStackHost(git=True)
    app.state.manager._clients[7] = host
    r = client.post("/api/instances/7/stacks/1/update", auth=BASIC)
    assert r.status_code == 200 and "not re-read from git" in r.json()["message"]


class ContainerHost:
    """A standalone container; recreate replaces it with a RUNNING one, as Portainer does."""
    instance = type("I", (), {"name": "p"})()

    def __init__(self, state):
        self.containers = [{"Id": "a" * 12, "Names": ["/tool"], "Image": "acme/tool:latest",
                            "ImageID": "sha256:x", "State": state, "Labels": {}}]
        self.stopped = []

    async def list_containers(self, endpoint_id):
        return self.containers

    async def recreate_container(self, endpoint_id, cid):
        self.containers = [{**self.containers[0], "Id": "b" * 12, "State": "running"}]

    async def set_container_state(self, endpoint_id, cid, running):
        assert running is False
        self.stopped.append(cid)

    async def get_container_info(self, endpoint_id, cid):
        return {"State": {"Running": True, "Restarting": False}, "RestartCount": 0}

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _fast_container_polls(monkeypatch):
    monkeypatch.setattr(main, "CONTAINER_POLL_SECONDS", 0.001)


def test_a_stopped_container_is_stopped_again_after_its_update(client):
    host = ContainerHost("exited")
    app.state.manager._clients[7] = host
    r = client.post(f"/api/instances/7/containers/{'a' * 12}/update?endpointId=3", auth=BASIC)
    assert r.status_code == 200, r.text
    assert host.stopped == ["b" * 12], "the replacement, found by name — its id is new"
    assert "stopped again" in r.json()["message"]


def test_a_running_container_is_left_running(client):
    host = ContainerHost("running")
    app.state.manager._clients[7] = host
    r = client.post(f"/api/instances/7/containers/{'a' * 12}/update?endpointId=3", auth=BASIC)
    assert r.status_code == 200 and host.stopped == []
    assert r.json()["message"] == "Repulled and recreated — running."


# ---------------------------------------------------------------- registry-aware pulls

@pytest.mark.parametrize("image,expected", [
    ("ghcr.io/acme/api:latest", 7),
    ("mariadb:10.11", 3),                    # Docker Hub, by any of its names
    ("acme/app:1", 3),
    ("registry.lan:5000/x:1", None),         # no login stored for it
    ("quay.io/x/y:1", None),                 # stored, but without authentication
])
def test_the_right_portainer_registry_is_chosen(image, expected):
    registries = [
        {"Id": 7, "URL": "ghcr.io", "Authentication": True},
        {"Id": 3, "URL": "https://registry-1.docker.io/", "Authentication": True},
        {"Id": 9, "URL": "quay.io", "Authentication": False},
        {"Id": "x", "URL": "registry.lan:5000", "Authentication": True},   # malformed id: ignored
    ]
    assert registry_id_for(image, registries) == expected


@pytest.mark.asyncio
async def test_pull_names_the_registry_the_way_portainers_own_ui_does():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("X-Registry-Auth")
        seen["image"] = request.url.params["fromImage"]
        return httpx.Response(200, text='{"status":"Status: Image is up to date"}\n')

    c = PortainerClient(RECORD, transport=httpx.MockTransport(handler))
    await c.pull_image(4, "ghcr.io/acme/api:latest", registry_id=7)
    assert json.loads(base64.b64decode(seen["auth"])) == {"registryId": 7}
    await c.pull_image(4, "mariadb:10.11")
    assert seen["auth"] is None and seen["image"] == "mariadb:10.11"
