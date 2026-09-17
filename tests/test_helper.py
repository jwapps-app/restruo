"""The helper replaces a container that cannot survive its own update.

Measured against a live Portainer: recreating an agent through Portainer
fails after the stop — "Stop container error: error during connect" — because
the stop kills the connection the create would have used. The helper is run
by the Docker daemon instead, so it outlives the thing it replaces. What has
to be right is what it does when something goes wrong halfway.
"""
import json

import httpx
import pytest

from app.helper import (
    Docker,
    HelperError,
    build_create_body,
    inherited_config,
    normalize_image_ref,
    replace,
)

OLD_IMG = "sha256:" + "a" * 64
NEW_IMG = "sha256:" + "b" * 64
OLD_ID = "0123456789ab" + "0" * 52

IMAGE_CONFIG = {
    "Cmd": None, "Entrypoint": ["./agent"], "WorkingDir": "/app",
    "Env": ["PATH=/app:/usr/bin"], "Labels": {"vendor": "Portainer.io", "rev": "old"},
    "ExposedPorts": {"9001/tcp": {}},
}


def agent_container():
    return {
        "Id": OLD_ID, "Name": "/portainer_agent", "Image": OLD_IMG,
        "Config": {
            "Image": "portainer/agent", "Hostname": OLD_ID[:12],
            "Cmd": None, "Entrypoint": ["./agent"], "WorkingDir": "/app",
            "Env": ["PATH=/app:/usr/bin", "AGENT_SECRET=s3cret"],
            "Labels": {"vendor": "Portainer.io", "rev": "old", "mine": "yes"},
            "ExposedPorts": {"9001/tcp": {}}, "MacAddress": "",
        },
        "HostConfig": {
            "NetworkMode": "bridge", "RestartPolicy": {"Name": "always"},
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock", "/:/host"],
            "PortBindings": {"9001/tcp": [{"HostIp": "", "HostPort": "9001"}]},
        },
        "NetworkSettings": {"Networks": {"bridge": {"Aliases": None, "IPAMConfig": None}}},
    }


class Engine:
    """A Docker Engine with just enough behaviour to rehearse a replacement."""

    def __init__(self, *, pull_error=None, new_stays_up=True, create_fails=False,
                 tag_points_to=NEW_IMG):
        self.pull_error, self.new_stays_up = pull_error, new_stays_up
        self.create_fails, self.tag_points_to = create_fails, tag_points_to
        self.log: list[str] = []
        self.created: dict | None = None
        self.names = {OLD_ID: "portainer_agent"}
        self.running = {OLD_ID: True}

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        parts = path.strip("/").split("/")
        if parts[0] == "images" and parts[1] == "create":
            self.log.append("pull")
            line = {"error": self.pull_error} if self.pull_error else {"status": "done"}
            return httpx.Response(200, text=json.dumps(line) + "\n")
        if parts[0] == "images":
            ref = "/".join(parts[1:-1])
            if ref == OLD_IMG:
                return httpx.Response(200, json={"Id": OLD_IMG, "Config": IMAGE_CONFIG})
            return httpx.Response(200, json={"Id": self.tag_points_to, "Config": IMAGE_CONFIG})
        if parts[0] == "containers" and parts[1] == "create":
            self.log.append("create")
            if self.create_fails:
                return httpx.Response(409, json={"message": "port is already allocated"})
            self.created = json.loads(request.content)
            self.names["NEWID"] = request.url.params["name"]
            self.running["NEWID"] = False
            return httpx.Response(201, json={"Id": "NEWID"})
        if parts[0] == "containers":
            ref = self._resolve(parts[1])
            action = parts[2] if len(parts) > 2 else ""
            if method == "DELETE":
                if ref is None:
                    return httpx.Response(404, json={"message": "no such container"})
                self.log.append(f"remove:{self.names[ref]}")
                del self.names[ref], self.running[ref]
                return httpx.Response(204)
            if ref is None:
                return httpx.Response(404, json={"message": "no such container"})
            if action == "json":
                if ref == OLD_ID:
                    return httpx.Response(200, json=agent_container())
                return httpx.Response(200, json={"State": {"Running": self.running[ref]}})
            if action == "stop":
                self.log.append(f"stop:{self.names[ref]}"); self.running[ref] = False
                return httpx.Response(204)
            if action == "start":
                self.log.append(f"start:{self.names[ref]}")
                self.running[ref] = self.new_stays_up if ref == "NEWID" else True
                return httpx.Response(204)
            if action == "rename":
                self.names[ref] = request.url.params["name"]
                self.log.append(f"rename:{self.names[ref]}")
                return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})

    def _resolve(self, ref):
        if ref in self.names:
            return ref
        return next((i for i, n in self.names.items() if n == ref), None)


def run(engine):
    return replace(Docker(engine.transport()), "portainer_agent", sleep=lambda s: None)


def test_successful_replacement_keeps_the_name_and_the_configuration():
    engine = Engine()
    message = run(engine)

    assert "Updated portainer_agent" in message
    assert engine.names == {"NEWID": "portainer_agent"}, "old one gone, new one has the name"
    assert engine.running["NEWID"] is True
    body = engine.created
    assert body["Image"] == "portainer/agent:latest"
    assert body["HostConfig"]["Binds"] == ["/var/run/docker.sock:/var/run/docker.sock", "/:/host"]
    assert body["HostConfig"]["PortBindings"]["9001/tcp"][0]["HostPort"] == "9001"
    assert body["HostConfig"]["RestartPolicy"] == {"Name": "always"}


def test_the_pull_happens_before_anything_is_stopped():
    engine = Engine()
    run(engine)
    assert engine.log.index("pull") < engine.log.index("stop:portainer_agent")


def test_failed_pull_touches_nothing():
    engine = Engine(pull_error="toomanyrequests: rate limit")
    with pytest.raises(HelperError, match="rate limit"):
        run(engine)
    assert engine.log == ["pull"]
    assert engine.running[OLD_ID] is True and engine.names[OLD_ID] == "portainer_agent"


def test_already_current_is_a_no_op():
    engine = Engine(tag_points_to=OLD_IMG)
    assert "already running" in run(engine)
    assert engine.log == ["pull"]
    assert engine.running[OLD_ID] is True


def test_new_container_that_dies_is_rolled_back():
    engine = Engine(new_stays_up=False)
    with pytest.raises(HelperError, match="rolled back"):
        run(engine)
    assert engine.names == {OLD_ID: "portainer_agent"}, "original has its name back"
    assert engine.running[OLD_ID] is True, "and is running"


def test_create_failure_is_rolled_back():
    engine = Engine(create_fails=True)
    with pytest.raises(HelperError, match="port is already allocated"):
        run(engine)
    assert engine.names == {OLD_ID: "portainer_agent"}
    assert engine.running[OLD_ID] is True


def test_config_inherited_from_the_old_image_is_not_pinned_onto_the_new_one():
    config = inherited_config(agent_container()["Config"], IMAGE_CONFIG)
    # The image's own start command, path and labels must come from the NEW image.
    assert "Entrypoint" not in config and "WorkingDir" not in config
    assert config["Env"] == ["AGENT_SECRET=s3cret"], "only what the operator set"
    assert config["Labels"] == {"mine": "yes"}
    assert "ExposedPorts" not in config


def test_generated_hostname_is_not_carried_over_but_a_chosen_one_is():
    body, _ = build_create_body(agent_container(), IMAGE_CONFIG)
    assert "Hostname" not in body
    chosen = agent_container(); chosen["Config"]["Hostname"] = "agent-1"
    body, _ = build_create_body(chosen, IMAGE_CONFIG)
    assert body["Hostname"] == "agent-1"


def test_several_networks_one_at_create_the_rest_connected_after():
    c = agent_container()
    c["HostConfig"]["NetworkMode"] = "front"
    c["NetworkSettings"]["Networks"] = {
        "front": {"Aliases": ["agent", OLD_ID[:12]], "IPAMConfig": {"IPv4Address": "172.20.0.9"}},
        "back": {"Aliases": ["agent"]},
    }
    body, extra = build_create_body(c, IMAGE_CONFIG)
    first = body["NetworkingConfig"]["EndpointsConfig"]["front"]
    assert first["IPAMConfig"] == {"IPv4Address": "172.20.0.9"}, "static address kept"
    assert first["Aliases"] == ["agent"], "the generated short-id alias is dropped"
    assert list(extra) == ["back"]


def test_host_network_gets_no_endpoint_config():
    c = agent_container(); c["HostConfig"]["NetworkMode"] = "host"
    body, extra = build_create_body(c, IMAGE_CONFIG)
    assert "NetworkingConfig" not in body and extra == {}


@pytest.mark.parametrize("ref,expected", [
    ("portainer/agent", "portainer/agent:latest"),
    ("portainer/agent:lts", "portainer/agent:lts"),
    ("registry.lan:5000/agent", "registry.lan:5000/agent:latest"),
    ("portainer/agent@sha256:abc", "portainer/agent@sha256:abc"),
])
def test_untagged_reference_means_latest_not_every_tag(ref, expected):
    assert normalize_image_ref(ref) == expected


@pytest.mark.asyncio
async def test_start_sends_an_empty_json_body():
    """Measured through a live agent: no body -> 400 "starting container with
    non-empty request body"; {} -> 204. Portainer's UI sends {} too."""
    from app.instances import InstanceRecord
    from app.portainer import PortainerClient
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(204)

    client = PortainerClient(
        InstanceRecord(id=1, name="p", base_url="https://p.test", auth_type="api_key", api_key="k"),
        transport=httpx.MockTransport(handler))
    await client.set_container_state(6, "abc", running=True)
    assert seen["body"] == b"{}"
