"""Did it actually work?

Portainer accepts a stack deploy and returns immediately, running compose in
the background — a redeploy the API "completed" in 68ms can still be pulling
an image a minute later. A recreate is answered as soon as the new container
has been started, whether or not it then stays up. So after either one, watch
until things settle, then judge what settled and report that.

Every watcher here returns {"ok": bool, "message": str}.
"""

import asyncio
import re
import time

from . import health, timing
from .errors import describe
from .portainer import PortainerClient, container_name, stack_containers

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


async def stack_containers_now(client: PortainerClient, stack: dict) -> list[dict] | None:
    """The stack's containers, or None when the listing itself failed — which
    says nothing about the stack and must not be read as "all gone"."""
    try:
        return stack_containers(stack, await client.list_containers(stack["EndpointId"]))
    except Exception:
        return None


def fingerprint(containers: list[dict]) -> frozenset:
    """Identity and state only. Docker's human-readable Status ("Up 5
    seconds") changes on every poll for a minute, which once made every
    deploy look like it took exactly that long to settle."""
    return frozenset((c.get("Id"), c.get("State"), c.get("ImageID")) for c in containers)


async def await_deploy(client: PortainerClient, stack: dict, before: frozenset) -> dict:
    """Block until the deploy settles, then judge it. ok means the stack's
    containers were replaced and are all working — nothing less is called a
    success."""
    deadline = time.monotonic() + timing.DEPLOY_TIMEOUT_SECONDS
    changed = False
    last = before
    last_containers: list[dict] = []
    stable = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(timing.DEPLOY_POLL_SECONDS)
        containers = await stack_containers_now(client, stack)
        if containers is None:
            continue
        current = fingerprint(containers)
        if current != last:
            changed, last, last_containers, stable = True, current, containers, 0
            continue
        last_containers = containers
        if changed:
            stable += 1
            if stable >= timing.DEPLOY_SETTLE_POLLS:
                break
    else:
        waited = int(timing.DEPLOY_TIMEOUT_SECONDS)
        if not changed:
            return {"ok": False, "message": (
                f"Portainer accepted the redeploy, but nothing about the stack's containers "
                f"changed in {waited}s — not confirmed.")}
        return {"ok": False, "message": (
            f"Still changing after {waited}s — the deploy may finish in "
            "the background, but it is not confirmed. Check the stack in Portainer.")}

    if not last_containers:
        return {"ok": False, "message": "The deploy settled with no containers in the stack."}
    broken = [container_name(c) for c in last_containers if not health.container_is_working(c)]
    if broken:
        return {"ok": False, "message": (
            f"Redeployed, but not working: {', '.join(sorted(broken))} "
            f"({'is' if len(broken) == 1 else 'are'} stopped, restarting, or unhealthy).")}
    recreated = len({c[0] for c in last} - {c[0] for c in before})
    return {"ok": True, "message": f"Redeployed {recreated} container{'' if recreated == 1 else 's'}, all running."}


async def last_log_line(client: PortainerClient, endpoint_id: int, cid: str) -> str:
    """What the container said last — usually the reason it died."""
    try:
        logs = await client.container_logs(endpoint_id, cid)
    except Exception:
        return ""
    lines = [_ANSI_RE.sub("", line).strip() for line in logs.splitlines()]
    lines = [line for line in lines if line]
    return lines[-1][:400] if lines else ""


async def _container_id_by_name(
    client: PortainerClient, endpoint_id: int, name: str
) -> str | None:
    """A recreated container has a new id; its name is what carries over."""
    return next(
        (c["Id"] for c in await client.list_containers(endpoint_id)
         if container_name(c) == name), None)


async def await_container(client: PortainerClient, endpoint_id: int, name: str) -> dict:
    """Watch a freshly recreated container until it has stayed up, or failed.

    A new image can refuse the old configuration and exit within seconds of
    starting — after Portainer has already answered that the recreate worked.
    A failure carries the container's last log line, which is usually the
    whole explanation.
    """
    cid: str | None = None
    stable = 0
    ran = False
    for _ in range(timing.CONTAINER_POLLS):
        await asyncio.sleep(timing.CONTAINER_POLL_SECONDS)
        try:
            if cid is None:
                cid = await _container_id_by_name(client, endpoint_id, name)
                if cid is None:
                    continue
            info = await client.get_container_info(endpoint_id, cid)
        except Exception:
            continue  # a failed poll says nothing about the container
        verdict = health.judge(info)
        # One that Docker has already had to restart is crash-looping, however
        # it looks at the instant of this poll.
        crashing = verdict == health.RESTARTING or health.has_restarted(info)

        if verdict == health.EXITED and health.exit_code(info) == 0 and not crashing:
            return {"ok": True,
                    "message": "Repulled and recreated. It ran and finished cleanly."}
        if verdict == health.EXITED or crashing or verdict == health.UNHEALTHY:
            if verdict == health.EXITED:
                what = f"exited right away (exit code {health.exit_code(info)})"
            elif crashing:
                what = "keeps crashing and restarting"
            else:
                what = "is running but failing its health check"
            said = await last_log_line(client, endpoint_id, cid)
            return {"ok": False,
                    "message": f"Recreated on the new image, but it {what}."
                               + (f" Its last log line: {said}" if said else "")}
        ran = True
        if verdict == health.STARTING:
            stable = 0
            continue
        stable += 1
        if stable >= timing.CONTAINER_STABLE_POLLS:
            return {"ok": True, "message": "Repulled and recreated — running."}
    if ran:
        # Up for the whole minute; only a slow health check is undecided.
        return {"ok": True, "message": "Repulled and recreated — running; its health "
                                       "check had not finished starting after a minute."}
    return {"ok": False,
            "message": f"Recreated, but “{name}” could not be found afterwards to confirm "
                       "it is running. Check it on that host."}


async def stop_again(client: PortainerClient, endpoint_id: int, name: str) -> str:
    """Stop the replacement of a container that was stopped before its update."""
    try:
        new_id = await _container_id_by_name(client, endpoint_id, name)
        if new_id is None:
            return ("Repulled and recreated — but the new container could not be found to "
                    "stop it again. It was stopped before; check it on that host.")
        await client.set_container_state(endpoint_id, new_id, running=False)
    except Exception as exc:
        return (f"Repulled and recreated — but stopping it again failed ({describe(exc)}). "
                "It was stopped before and is running now.")
    return ("Repulled and recreated. It was stopped before, so it was stopped again — "
            "Portainer's recreate starts a container, so it ran for a moment.")
