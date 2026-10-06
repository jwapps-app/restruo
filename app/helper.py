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
import secrets
import sys
import time

import httpx

DOCKER_SOCKET = "/var/run/docker.sock"
OLD_SUFFIX = "-restruo-old"
# Let the request that started this container finish its round trip before
# the container carrying that request is stopped.
STARTUP_GRACE_SECONDS = 3.0
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


def _covered_destinations(host_config: dict) -> set[str]:
    """Container paths that HostConfig already provides a mount for."""
    covered: set[str] = set()
    for bind in host_config.get("Binds") or []:
        parts = bind.split(":")
        # "src:dst[:opts]"; a bare "dst" is an anonymous volume asked for by -v
        covered.add(parts[1] if len(parts) >= 2 else parts[0])
    for mount in host_config.get("Mounts") or []:
        if mount.get("Target"):
            covered.add(mount["Target"])
    return covered


def mounts_to_carry(container: dict) -> list[dict]:
    """Mounts the replacement must be given explicitly or it will lose them.

    A volume that came from the image's VOLUME line, with no -v naming it,
    appears nowhere in HostConfig — only in the live container's Mounts. A
    replacement created from HostConfig alone gets a fresh empty volume in its
    place, and for Portainer that is a blank install with no environments and
    no users. Name every such volume so the new container reattaches to the
    same data.
    """
    covered = _covered_destinations(container.get("HostConfig") or {})
    out = []
    for mount in container.get("Mounts") or []:
        dest = mount.get("Destination")
        if not dest or dest in covered:
            continue
        kind = mount.get("Type")
        source = mount.get("Name") if kind == "volume" else mount.get("Source")
        if not source or kind not in ("volume", "bind"):
            continue
        out.append({"Type": kind, "Source": source, "Target": dest,
                    "ReadOnly": not mount.get("RW", True)})
    return out


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
    carried = mounts_to_carry(container)
    if carried:
        host_config["Mounts"] = list(host_config.get("Mounts") or []) + carried
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


READY_POLL_SECONDS = 2.0
READY_POLLS = 45            # 90 s for a healthcheck to pass
READY_STABLE_POLLS = 4      # running (and healthy, if checked) this long in a row


def wait_ready(docker: Docker, ref: str, sleep=time.sleep) -> None:
    """Block until the container has proven itself, or raise.

    Running is not the same as working. A process can stay up while its
    healthcheck fails, or crash a few seconds in. So: it must be running,
    not restarting, with its healthcheck — when it has one — reporting
    healthy, and it must hold that for several polls in a row.
    """
    stable = 0
    for _ in range(READY_POLLS):
        sleep(READY_POLL_SECONDS)
        state = docker.inspect_container(ref).get("State") or {}
        if not state.get("Running") or state.get("Restarting"):
            code = state.get("ExitCode")
            raise HelperError(f"the new container stopped (exit code {code})")
        health = (state.get("Health") or {}).get("Status")
        if health == "unhealthy":
            raise HelperError("the new container is running but reports unhealthy")
        if health == "starting":
            stable = 0
            continue
        stable += 1
        if stable >= READY_STABLE_POLLS:
            return
    raise HelperError("the new container did not become healthy in time")


def replace(docker: Docker, target: str, sleep=time.sleep) -> str:
    """Swap `target` for a container on the freshly pulled image. Returns a
    description of what happened. Raises HelperError if it could not be done;
    the message says whether the original is running again, and under what
    name, because recovery itself can fail part-way."""
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
    # A name of our own for the set-aside original: unique, so nothing that
    # happens to carry the obvious name is ever deleted on the strength of it.
    parked = f"{name}{OLD_SUFFIX}-{secrets.token_hex(3)}"

    done = {"stopped": False, "parked": False, "created": None}
    try:
        docker.stop(old_id)
        done["stopped"] = True
        docker.rename(old_id, parked)
        done["parked"] = True
        done["created"] = docker.create(name, body)
        for network, settings in extra_networks.items():
            docker.connect(network, done["created"], settings)
        docker.start(done["created"])
        wait_ready(docker, done["created"], sleep)
    except Exception as exc:
        raise HelperError(_recover(docker, exc, name, old_id, parked, done)) from exc

    docker.remove(old_id)
    return f"Updated {name} to {new_image_id.removeprefix('sha256:')[:12]}."


def _recover(docker: Docker, cause: Exception, name: str, old_id: str, parked: str,
             done: dict) -> str:
    """Undo every step that was taken, attempting each even if another fails,
    and say exactly what state things were left in."""
    problems = []
    if done["created"] is not None:
        try:
            docker.remove(done["created"], force=True)
        except Exception as exc:
            problems.append(f"could not remove the new container: {exc}")
    restored_name = not done["parked"]
    if done["parked"]:
        try:
            docker.rename(old_id, name)
            restored_name = True
        except Exception as exc:
            problems.append(f"could not restore the name: {exc}")
    running = False
    if done["stopped"]:
        try:
            docker.start(old_id)
            running = True
        except Exception as exc:
            problems.append(f"could not restart the original: {exc}")
    else:
        running = True
    where = name if restored_name else parked
    if not problems:
        return f"{cause} — rolled back, {name} is running its previous image"
    state = "running" if running else "STOPPED"
    return (f"{cause} — rollback incomplete: {'; '.join(problems)}. "
            f"The original container is {state} under the name {where!r} on that host.")


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
