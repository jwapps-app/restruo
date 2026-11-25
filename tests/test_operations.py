"""What may run at once, and the jobs that outlive their request."""

import asyncio

import pytest

from app import timing
from app.errors import Busy
from app.operations import Operations, Timed, stack_key


@pytest.mark.asyncio
async def test_quick_work_comes_back_finished_and_frees_its_target():
    ops = Operations()

    async def work():
        return {"ok": True, "message": "done"}

    job = await ops.run(stack_key(5, 1, 42), work)

    assert job.done and job.result == {"ok": True, "message": "done"}
    assert ops.in_flight == set()
    assert ops.job(job.id) is job


@pytest.mark.asyncio
async def test_slow_work_comes_back_running_and_holds_its_target(monkeypatch):
    monkeypatch.setattr(timing, "JOB_SYNC_WAIT_SECONDS", 0.01)
    ops = Operations()
    release = asyncio.Event()

    async def work():
        await release.wait()
        return {"ok": True, "message": "done"}

    job = await ops.run(stack_key(5, 1, 42), work)
    assert not job.done and stack_key(5, 1, 42) in ops.in_flight

    # Refused before anything starts, with the reason — not as a failed job.
    with pytest.raises(Busy) as refused:
        await ops.run(stack_key(5, 1, 42), work)
    assert "already running" in refused.value.detail
    assert len(ops.jobs) == 1

    release.set()
    await job.task
    assert job.done and job.result["ok"] and ops.in_flight == set()


@pytest.mark.asyncio
async def test_work_that_raises_is_a_failed_result_not_a_lost_job():
    ops = Operations()

    async def work():
        raise RuntimeError("proxy fell over")

    job = await ops.run(stack_key(5, 1, 42), work)

    assert job.done and job.result == {"ok": False, "message": "RuntimeError: proxy fell over"}
    assert ops.in_flight == set(), "a failure must not leave the target locked"


@pytest.mark.asyncio
async def test_finished_jobs_are_forgotten_after_a_while(monkeypatch):
    ops = Operations()

    async def work():
        return {"ok": True, "message": "done"}

    job = await ops.run(stack_key(5, 1, 42), work)
    monkeypatch.setattr(timing, "JOB_RETENTION_SECONDS", -1)
    assert ops.job(job.id) is None


def test_every_action_answers_in_one_shape():
    result = Timed("jellyfin").fail("could not pull")
    assert result["ok"] is False and result["name"] == "jellyfin"
    assert result["message"] == "could not pull" and isinstance(result["durationMs"], int)
    assert set(Timed("x").ok("fine")) == {"ok", "name", "durationMs", "message"}
