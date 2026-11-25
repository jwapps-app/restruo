"""What is running right now, and what may run beside it.

One Operations object per process holds the two pieces of state every action
shares: the targets something is being done to (so nothing pulls the ground
from under it), and the jobs that outlived the request that started them.
Nothing here knows about HTTP; a refusal is a Busy error with its reason.
"""

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from . import timing
from .errors import Busy, Refused, describe

# Portainer waits on the new containers before it answers, which is why "an
# update is already running" can stay true for a minute.
WAIT_HINT = " It takes a moment while Portainer waits for the containers to come up."


def stack_key(iid: int, endpoint_id: int, stack_id: int) -> tuple:
    return ("stack", iid, endpoint_id, stack_id)


def container_key(iid: int, endpoint_id: int, container_id: str) -> tuple:
    return ("container", iid, endpoint_id, container_id)


def helper_key(kind: str, iid: int, endpoint_id: int, container_id: str) -> tuple:
    """`kind` is "agent" or "portainer" — what the helper is replacing."""
    return (kind, iid, endpoint_id, container_id)


def prune_key(iid: int) -> tuple:
    return ("prune", iid)


def conflicts(key: tuple, held: tuple) -> str | None:
    """Why `key` cannot run while `held` is in flight, or None if it can.

    Keys: ("stack", iid, eid, sid), ("container", iid, eid, cid),
    ("agent", iid, eid, cid) for replacing the environment's own agent,
    ("portainer", iid, …) for replacing Portainer itself, ("prune", iid).
    """
    if key == held:
        return "an update is already running for this one"
    if key[1] != held[1]:
        return None  # different instances never conflict
    kinds = {key[0], held[0]}
    if "portainer" in kinds:
        return "Portainer itself is being replaced on this instance"
    if "prune" in kinds:
        return "a clean-up is running on this instance" if held[0] == "prune" \
            else "an update is running on this instance"
    if "agent" in kinds and key[2] == held[2]:
        # Every stack on an environment deploys through its agent.
        return "this environment's agent is being replaced" if held[0] == "agent" \
            else "a deploy is running on this environment"
    return None


class Timed:
    """Times one action on one named target, and builds the result every
    action answers with: {"ok", "name", "durationMs", "message"}."""

    def __init__(self, name: str):
        self.name = name
        self._started = time.monotonic()

    def result(self, ok: bool, message: str) -> dict:
        return {
            "ok": ok,
            "name": self.name,
            "durationMs": int((time.monotonic() - self._started) * 1000),
            "message": message,
        }

    def ok(self, message: str) -> dict:
        return self.result(True, message)

    def fail(self, message: str) -> dict:
        return self.result(False, message)


@dataclass
class Job:
    id: str
    started: float
    done: bool = False
    result: dict | None = None
    # Held so the running task is never garbage-collected mid-deploy.
    task: asyncio.Task | None = field(default=None, repr=False)


class Operations:
    def __init__(self) -> None:
        self.in_flight: set[tuple] = set()
        self.jobs: dict[str, Job] = {}

    def _claim(self, key: tuple, hint: str = "") -> None:
        for held in self.in_flight:
            reason = conflicts(key, held)
            if reason:
                raise Busy(f"Not now — {reason}.{hint}")
        self.in_flight.add(key)

    @asynccontextmanager
    async def exclusive(self, key: tuple):
        """One operation at a time per target — and none that would pull the
        ground from under another: no stack deploy while its environment's
        agent is being replaced, nothing at all while Portainer itself is, no
        prune while anything deploys.

        Portainer refuses a second deploy of the same stack while one is
        running, and answers with a generic "Unable to update stack" — which
        reads as a failure of the update rather than of the timing. A redeploy
        can take a minute while compose waits on a healthcheck, which is ample
        time to click again, or to click from another tab.
        """
        self._claim(key, WAIT_HINT)
        try:
            yield
        finally:
            self.in_flight.discard(key)

    async def run(self, key: tuple, work) -> Job:
        """Run `work()` with `key` held, and wait a short while for it.

        Raises Busy before anything starts if the target is taken. Otherwise
        returns the job — finished, with its result, when the work was quick;
        still running when it was not, for the caller to hand out its id.
        """
        self._claim(key)
        self._forget_old_jobs()
        job = Job(id=secrets.token_hex(8), started=time.time())
        self.jobs[job.id] = job

        async def runner() -> None:
            try:
                job.result = await work()
            except Refused as exc:
                job.result = {"ok": False, "message": exc.detail}
            except Exception as exc:
                job.result = {"ok": False, "message": describe(exc)}
            finally:
                job.done = True
                self.in_flight.discard(key)

        job.task = asyncio.create_task(runner())
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timing.JOB_SYNC_WAIT_SECONDS)
        except asyncio.TimeoutError:
            pass
        return job

    def job(self, job_id: str) -> Job | None:
        self._forget_old_jobs()
        return self.jobs.get(job_id)

    def _forget_old_jobs(self) -> None:
        cutoff = time.time() - timing.JOB_RETENTION_SECONDS
        for job_id in [j for j, job in self.jobs.items() if job.done and job.started < cutoff]:
            del self.jobs[job_id]
