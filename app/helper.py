"""Replace a container that cannot survive its own update.

Portainer relays every Docker call for an environment through that
environment's agent, and performs its own calls from its own container. A
recreate of either one therefore stops the thing carrying the command: the
stop succeeds, the connection dies, and the create that should follow is never
sent.

This module is the way round that. Restruo starts it as a short-lived
container on the target host, with the Docker socket mounted. The Docker
daemon runs it — not the agent, not Portainer — so it keeps going while they
are down. It pulls first, so nothing is touched if the download fails, and it
puts the old container back if the new one does not stay up.

    python -m app.helper <container id or name>

The last line it prints is a JSON object: {"ok": bool, "message": str}.
Exit status is 0 on success (including "already current") and 1 otherwise.
"""

import json
import sys
import time

import httpx

DOCKER_SOCKET = "/var/run/docker.sock"
OLD_SUFFIX = "-restruo-old"
# Let the request that started this container finish its round trip before
# the container carrying that request is stopped.
STARTUP_GRACE_SECONDS = 3.0
# How long the replacement must stay up before the old one is discarded.
SETTLE_SECONDS = 8.0
STOP_TIMEOUT_SECONDS = 20

# Config fields a container inherits from its image. Where the old container
# merely carried the old image's value, the new image's value should win.
_INHERITED_SCALARS = ("Cmd", "Entrypoint", "WorkingDir", "User", "StopSignal",
                      "Healthcheck", "Shell")
_INHERITED_MAPS = ("Labels", "ExposedPorts", "Volumes")


class HelperError(Exception):
    pass


class Docker:
    """The few Docker Engine calls this needs, over the local socket."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            transport=transport or httpx.HTTPTransport(uds=DOCKER_SOCKET),
            base_url="http://docker",
            timeout=httpx.Timeout(30.0, read=1800.0),  # a pull can be slow
        )

    def _check(self, response: httpx.Response, what: str) -> httpx.Response:
        if response.is_error:
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            raise HelperError(f"{what}: {detail.strip()}")
        return response

    def inspect_container(self, ref: str) -> dict:
        return self._check(self._client.get(f"/containers/{ref}/json"), "inspect").json()

    def inspect_image(self, ref: str) -> dict:
        return self._check(self._client.get(f"/images/{ref}/json"), "inspect image").json()

    def pull(self, image: str) -> None:
        response = self._check(
            self._client.post("/images/create", params={"fromImage": image}), "pull"
        )
        # The status is 200 even when the pull fails; the error is in the stream.
        for line in response.text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("error"):
                raise HelperError(f"pull {image}: {event['error']}")

    def stop(self, ref: str) -> None:
        response = self._client.post(
            f"/containers/{ref}/stop", params={"t": STOP_TIMEOUT_SECONDS}
        )
        if response.status_code != 304:  # already stopped
            self._check(response, "stop")

    def start(self, ref: str) -> None:
        response = self._client.post(f"/containers/{ref}/start")
        if response.status_code != 304:
            self._check(response, "start")

    def rename(self, ref: str, name: str) -> None:
        self._check(
            self._client.post(f"/containers/{ref}/rename", params={"name": name}), "rename"
        )

    def create(self, name: str, body: dict) -> str:
        response = self._check(
            self._client.post("/containers/create", params={"name": name}, json=body),
            "create",
        )
        return response.json()["Id"]

    def connect(self, network: str, container: str, endpoint: dict) -> None:
        self._check(
            self._client.post(
                f"/networks/{network}/connect",
                json={"Container": container, "EndpointConfig": endpoint},
            ),
            f"connect to {network}",
        )

    def remove(self, ref: str, force: bool = False) -> None:
        response = self._client.delete(
            f"/containers/{ref}", params={"force": "1" if force else "0"}
        )
        if response.status_code != 404:
            self._check(response, "remove")


def normalize_image_ref(ref: str) -> str:
    """`portainer/agent` means `portainer/agent:latest`; without the tag the
    Engine would pull every tag in the repository."""
    if "@" in ref:
        return ref
    last = ref.rsplit("/", 1)[-1]
    return ref if ":" in last else f"{ref}:latest"


def inherited_config(config: dict, old_image_config: dict) -> dict:
    """The container's Config with everything it only inherited stripped out.

    Copying Config wholesale would pin the old image's Cmd, Entrypoint and
    environment onto the new image — a new release that changes its start
    command or bumps a version variable would be started the old way.
    """
    out = dict(config)
    for key in _INHERITED_SCALARS:
        if out.get(key) == old_image_config.get(key):
            out.pop(key, None)
    for key in _INHERITED_MAPS:
        mine, theirs = out.get(key) or {}, old_image_config.get(key) or {}
        kept = {k: v for k, v in mine.items() if k not in theirs or theirs[k] != v}
        if kept:
            out[key] = kept
        else:
            out.pop(key, None)
    image_env = set(old_image_config.get("Env") or [])
    env = [e for e in (out.get("Env") or []) if e not in image_env]
    if env:
        out["Env"] = env
    else:
        out.pop("Env", None)
    return out


def endpoint_settings(network: dict, old_id: str) -> dict:
    """What to ask for when rejoining a network: the static address and the
    aliases that were configured, not the ones Docker generated."""
    settings: dict = {}
    if network.get("IPAMConfig"):
        settings["IPAMConfig"] = network["IPAMConfig"]
    if network.get("Links"):
        settings["Links"] = network["Links"]
    aliases = [a for a in (network.get("Aliases") or []) if a != old_id[:12]]
    if aliases:
        settings["Aliases"] = aliases
    if network.get("DriverOpts"):
        settings["DriverOpts"] = network["DriverOpts"]
    return settings


def build_create_body(container: dict, old_image_config: dict) -> tuple[dict, dict]:
    """Returns (create body, networks to join after creation)."""
    old_id = container["Id"]
    config = inherited_config(container.get("Config") or {}, old_image_config)
    config["Image"] = normalize_image_ref((container.get("Config") or {}).get("Image", ""))
    # Docker sets the hostname to the container's short id unless told
    # otherwise; carrying that over would name the new one after the old.
    if config.get("Hostname") == old_id[:12]:
        config.pop("Hostname", None)
    if not config.get("MacAddress"):
        config.pop("MacAddress", None)

    host_config = dict(container.get("HostConfig") or {})
    body = {**config, "HostConfig": host_config}

    mode = host_config.get("NetworkMode") or "default"
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    extra: dict = {}
    if mode in ("host", "none") or mode.startswith("container:"):
        return body, extra
    # One network at creation — every Engine version accepts that — and the
    # rest joined before it starts.
    first = mode if mode in networks else next(iter(networks), None)
    if first is not None:
        body["NetworkingConfig"] = {
            "EndpointsConfig": {first: endpoint_settings(networks[first], old_id)}
        }
        extra = {
            name: endpoint_settings(net, old_id)
            for name, net in networks.items() if name != first
        }
    return body, extra


def _is_up(docker: Docker, ref: str) -> bool:
    state = docker.inspect_container(ref).get("State") or {}
    return bool(state.get("Running")) and not state.get("Restarting")


def replace(docker: Docker, target: str, sleep=time.sleep) -> str:
    """Swap `target` for a container on the freshly pulled image. Returns a
    description of what happened; raises HelperError if it could not be done,
    in which case the original container is running again."""
    container = docker.inspect_container(target)
    old_id = container["Id"]
    name = container["Name"].lstrip("/")
    image = normalize_image_ref((container.get("Config") or {}).get("Image", ""))
    if not image or image.startswith("sha256:"):
        raise HelperError(f"{name} was created from an image id, not a name — nothing to pull")

    old_image_config = {}
    try:
        old_image_config = docker.inspect_image(container["Image"]).get("Config") or {}
    except HelperError:
        pass  # image already gone: copy the config as it stands

    docker.pull(image)  # before anything is touched
    new_image_id = docker.inspect_image(image)["Id"]
    if new_image_id == container["Image"]:
        return f"{name} is already running the current {image}."

    body, extra_networks = build_create_body(container, old_image_config)
    parked = f"{name}{OLD_SUFFIX}"
    docker.remove(parked, force=True)  # left over from an interrupted run

    docker.stop(old_id)
    docker.rename(old_id, parked)
    new_id = None
    try:
        new_id = docker.create(name, body)
        for network, settings in extra_networks.items():
            docker.connect(network, new_id, settings)
        docker.start(new_id)
        sleep(SETTLE_SECONDS)
        if not _is_up(docker, new_id):
            raise HelperError("the new container did not stay up")
    except Exception as exc:
        # Put things back exactly as they were.
        if new_id is not None:
            docker.remove(new_id, force=True)
        docker.rename(old_id, name)
        docker.start(old_id)
        raise HelperError(f"{exc} — rolled back, {name} is running its previous image") from exc

    docker.remove(old_id)
    return f"Updated {name} to {new_image_id.removeprefix('sha256:')[:12]}."


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"ok": False, "message": "usage: python -m app.helper <container>"}))
        return 1
    time.sleep(STARTUP_GRACE_SECONDS)
    try:
        message = replace(Docker(), argv[1])
    except Exception as exc:
        print(json.dumps({"ok": False, "message": str(exc) or type(exc).__name__}))
        return 1
    print(json.dumps({"ok": True, "message": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
