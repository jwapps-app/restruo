"""Is this container working?

Two sources say, and they speak differently. An *inspect* of one container
has structured state (Running, Restarting, ExitCode, Health); the container
*list* has only a state word and a human-readable status line. Each gets one
reading here, used by everything that asks — the dashboard's red dot, the
deploy watcher, the container watcher and the helper.

No imports beyond the standard library: the helper runs this from a bare
container on another host.
"""

import re

# What an inspected container is doing right now.
WORKING = "working"          # running, and healthy if it has a healthcheck
STARTING = "starting"        # running; its healthcheck has not decided yet
UNHEALTHY = "unhealthy"      # running, failing its healthcheck
RESTARTING = "restarting"    # between a crash and the next attempt
EXITED = "exited"            # not running

# List states that mean "not running, and not about to be".
STOPPED_STATES = ("exited", "created", "dead")

_EXIT_CODE_RE = re.compile(r"Exited \((\d+)\)")


def judge(info: dict) -> str:
    """Read a container inspect. Whether EXITED is a failure is the caller's
    call — a one-shot job that exits 0 did what it was for — so the exit code
    is read separately with exit_code()."""
    state = info.get("State") or {}
    if state.get("Restarting"):
        return RESTARTING
    if not state.get("Running"):
        return EXITED
    health = (state.get("Health") or {}).get("Status")
    if health == "unhealthy":
        return UNHEALTHY
    if health == "starting":
        return STARTING
    return WORKING


def exit_code(info: dict):
    return (info.get("State") or {}).get("ExitCode")


def has_restarted(info: dict) -> bool:
    """Docker has had to restart it at least once since it was created."""
    return (info.get("RestartCount") or 0) > 0


# --- the container list -------------------------------------------------------


def is_stopped(container: dict) -> bool:
    """Listed as not running — as opposed to running badly."""
    return (container.get("State") or "").lower() in STOPPED_STATES


def container_is_down(container: dict) -> bool:
    """Not running, or running but failing its healthcheck."""
    state = (container.get("State") or "").lower()
    status = container.get("Status") or ""
    return state != "running" or "(unhealthy)" in status


def container_is_working(container: dict) -> bool:
    """Running and not failing its healthcheck — or finished cleanly, which
    is what a one-shot service (a backup job, a migration) is meant to do."""
    state = (container.get("State") or "").lower()
    status = container.get("Status") or ""
    if state == "running":
        return "(unhealthy)" not in status
    if state == "exited":
        match = _EXIT_CODE_RE.search(status)
        return bool(match) and match.group(1) == "0"
    return False
