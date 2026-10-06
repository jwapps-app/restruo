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
                 tag_points_to=NEW_IMG, health=None, rename_fails=False,
                 remove_new_fails=False, extra_containers=None, old=None):
        self.pull_error, self.new_stays_up = pull_error, new_stays_up
        self.create_fails, self.tag_points_to = create_fails, tag_points_to
        # health: a list of statuses the new container reports, one per poll;
        # the last one repeats. None = no healthcheck.
        self.health, self.polls = health, 0
        self.rename_fails, self.remove_new_fails = rename_fails, remove_new_fails
        self.log: list[str] = []
        self.created: dict | None = None
        self.old = old or agent_container()
        self.names = {OLD_ID: "portainer_agent"}
        self.running = {OLD_ID: True}
        for cid, cname in (extra_containers or {}).items():
            self.names[cid] = cname
            self.running[cid] = True

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _new_state(self):
        state = {"Running": self.running["NEWID"], "Restarting": False, "ExitCode": 0 if self.running["NEWID"] else 1}
        if self.health is not None and self.running["NEWID"]:
            status = self.health[min(self.polls, len(self.health) - 1)]
            self.polls += 1
            state["Health"] = {"Status": status}
        return state

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
            if request.url.params["name"] in self.names.values():
                return httpx.Response(409, json={"message": "name already in use"})
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
                if ref == "NEWID" and self.remove_new_fails:
                    return httpx.Response(500, json={"message": "device or resource busy"})
                self.log.append(f"remove:{self.names[ref]}")
                del self.names[ref], self.running[ref]
                return httpx.Response(204)
            if ref is None:
                return httpx.Response(404, json={"message": "no such container"})
            if action == "json":
                if ref == OLD_ID:
                    return httpx.Response(200, json=self.old)
                if ref == "NEWID":
                    return httpx.Response(200, json={"State": self._new_state()})
                return httpx.Response(200, json={"State": {"Running": self.running[ref]}})
            if action == "stop":
                self.log.append(f"stop:{self.names[ref]}"); self.running[ref] = False
                return httpx.Response(204)
            if action == "start":
                self.log.append(f"start:{self.names[ref]}")
                self.running[ref] = self.new_stays_up if ref == "NEWID" else True
                return httpx.Response(204)
            if action == "rename":
                if self.rename_fails and ref == OLD_ID and "restruo-old" in request.url.params["name"]:
                    return httpx.Response(500, json={"message": "rename failed"})
                if request.url.params["name"] in self.names.values():
                    return httpx.Response(409, json={"message": "name already in use"})
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


# --- audit A01: anonymous volumes travel with the container ------------------

def test_anonymous_volume_is_reattached_not_recreated():
    """`docker run portainer/portainer-ce` with no -v gives /data an anonymous
    volume that appears only in the live container's Mounts. The replacement
    must be told its name, or it starts with an empty one — a blank Portainer."""
    c = agent_container()
    c["Config"]["Volumes"] = {"/data": {}}
    c["HostConfig"]["Binds"] = ["/var/run/docker.sock:/var/run/docker.sock"]
    c["Mounts"] = [
        {"Type": "volume", "Name": "9f3c2e…existing-data", "Destination": "/data", "RW": True},
        {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
    ]
    body, _ = build_create_body(c, {**IMAGE_CONFIG, "Volumes": {"/data": {}}})
    mounts = body["HostConfig"]["Mounts"]
    assert mounts == [{"Type": "volume", "Source": "9f3c2e…existing-data", "Target": "/data", "ReadOnly": False}]
    assert body["HostConfig"]["Binds"] == ["/var/run/docker.sock:/var/run/docker.sock"], "bind untouched"


def test_mounts_already_in_hostconfig_are_not_duplicated():
    c = agent_container()
    c["HostConfig"]["Mounts"] = [{"Type": "volume", "Source": "named", "Target": "/data"}]
    c["Mounts"] = [{"Type": "volume", "Name": "named", "Destination": "/data", "RW": True},
                   {"Type": "bind", "Source": "/", "Destination": "/host", "RW": True}]
    body, _ = build_create_body(c, IMAGE_CONFIG)
    assert body["HostConfig"]["Mounts"] == [{"Type": "volume", "Source": "named", "Target": "/data"}]


# --- audit A02: recovery attempts every step and says what it managed --------

def test_rename_failure_still_restarts_the_original():
    engine = Engine(rename_fails=True)
    with pytest.raises(HelperError, match="rolled back"):
        run(engine)
    assert engine.running[OLD_ID] is True
    assert engine.names[OLD_ID] == "portainer_agent"


def test_incomplete_recovery_is_reported_honestly():
    """The new container will not die; the old one must still be restarted,
    and the message must say which name it is under."""
    engine = Engine(new_stays_up=False, remove_new_fails=True)
    with pytest.raises(HelperError) as caught:
        run(engine)
    message = str(caught.value)
    assert "rollback incomplete" in message
    assert engine.running[OLD_ID] is True, "restart was still attempted"
    assert "restruo-old" in message, "names the parked container"
    assert "running" in message


# --- audit A03: nothing is deleted on the strength of a name -----------------

def test_an_unrelated_container_with_the_obvious_name_survives():
    engine = Engine(extra_containers={"BYSTANDER": "portainer_agent-restruo-old"})
    run(engine)
    assert "BYSTANDER" in engine.names, "never removed"
    assert engine.running["BYSTANDER"] is True


def test_parked_name_is_unique_per_run():
    first, second = Engine(), Engine()
    run(first); run(second)
    parked = lambda e: next(x for x in e.log if x.startswith("rename:"))
    assert parked(first) != parked(second)


# --- audit A04: running is not the same as working ---------------------------

def test_unhealthy_replacement_is_rolled_back():
    engine = Engine(health=["starting", "starting", "unhealthy"])
    with pytest.raises(HelperError, match="unhealthy"):
        run(engine)
    assert engine.names == {OLD_ID: "portainer_agent"} and engine.running[OLD_ID]


def test_healthcheck_is_waited_for_then_trusted():
    engine = Engine(health=["starting", "starting", "starting", "healthy"])
    assert "Updated" in run(engine)
    assert engine.names == {"NEWID": "portainer_agent"}


def test_a_container_that_never_becomes_healthy_is_rolled_back():
    engine = Engine(health=["starting"])
    with pytest.raises(HelperError, match="did not become healthy"):
        run(engine)
    assert engine.running[OLD_ID] is True
