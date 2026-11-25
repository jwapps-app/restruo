"""Start the update helper on a host, and read back what it did.

app/helper.py is the program; this is the side that launches it. Portainer
cannot recreate itself or one of its agents — the command travels through
the container being replaced — so the job is handed to a short-lived
container the Docker daemon runs, which outlives both.
"""

import asyncio
import json
import logging
import secrets
import time

from . import timing
from .errors import describe
from .helper import DOCKER_SOCKET
from .portainer import PortainerClient

logger = logging.getLogger("restruo.helper")

HELPER_LABEL = "restruo.helper"
DEFAULT_REPOSITORY = "ghcr.io/jwapps-app/restruo"


def default_helper_image(version: str) -> str:
    """The same build as this dashboard, so the helper and the code that
    reads its answer can never disagree. Every build is pushed under its
    commit as well as :latest."""
    tag = version if version and version != "dev" else "latest"
    return f"{DEFAULT_REPOSITORY}:{tag}"


def helper_spec(image: str, target: str) -> dict:
    """A container that can reach the Docker socket and nothing else: no
    network, no environment, and it replaces exactly one named container."""
    return {
        "Image": image,
        "Entrypoint": ["python", "-m", "app.helper"],
        "Cmd": [target],
        "User": "0",  # the socket is root's
        "Env": [],
        "Labels": {HELPER_LABEL: "1"},
        "HostConfig": {
            "Binds": [f"{DOCKER_SOCKET}:{DOCKER_SOCKET}"],
            "NetworkMode": "none",
            "AutoRemove": False,  # its exit status and last line are the result
        },
    }


def parse_helper_result(logs: str, exit_code: int | None) -> dict:
    for line in reversed(logs.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return {"ok": bool(parsed["ok"]), "message": str(parsed.get("message", ""))}
    tail = logs.strip().splitlines()[-1] if logs.strip() else "no output"
    return {"ok": False, "message": f"The helper exited with status {exit_code}: {tail}"}


async def _remove_helper(client: PortainerClient, endpoint_id: int, helper_id: str) -> None:
    """Remove a helper we started — and only that: the id is checked for the
    label the helper is created with before anything is deleted."""
    try:
        info = await client.get_container_info(endpoint_id, helper_id)
        labels = ((info.get("Config") or {}).get("Labels") or {})
        if labels.get(HELPER_LABEL) != "1":
            logger.warning("Not removing %s: it does not carry the helper label", helper_id[:12])
            return
        await client.remove_container(endpoint_id, helper_id, force=True)
    except Exception as exc:
        logger.warning("Could not remove helper %s: %s", helper_id[:12], describe(exc))


async def replace_with_helper(
    client: PortainerClient, endpoint_id: int, cid: str, name: str, image: str
) -> dict:
    """Replace container `cid` using a helper run from `image`. Returns
    {"ok", "message"}."""
    # A name of our own, unique per run: nothing is ever removed on the
    # strength of a predictable name, and two runs cannot collide.
    helper_name = f"restruo-helper-{cid[:12]}-{secrets.token_hex(3)}"
    try:
        await client.pull_image(endpoint_id, image)
        helper_id = await client.create_container(
            endpoint_id, helper_name, helper_spec(image, cid)
        )
        await client.set_container_state(endpoint_id, helper_id, running=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not start the update helper: {describe(exc)}"}

    # From here the agent — or Portainer itself — goes away and comes back, so
    # a failed poll is expected and means nothing. Only the helper's own exit
    # says how it went.
    deadline = time.monotonic() + timing.HELPER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(timing.HELPER_POLL_SECONDS)
        try:
            state = (await client.get_container_info(endpoint_id, helper_id)).get("State") or {}
        except Exception:
            continue
        if state.get("Running") or state.get("Status") in ("created", "restarting"):
            continue
        try:
            logs = await client.container_logs(endpoint_id, helper_id)
        except Exception:
            logs = ""
        result = parse_helper_result(logs, state.get("ExitCode"))
        await _remove_helper(client, endpoint_id, helper_id)
        return result
    return {"ok": False,
            "message": f"Lost track of the update after "
                       f"{int(timing.HELPER_TIMEOUT_SECONDS / 60)} "
                       f"minutes — check “{name}” on that host."}
