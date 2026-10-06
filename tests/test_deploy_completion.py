"""A stack update must report the deploy, not its acceptance — and judge it.

Portainer's stack API is asynchronous: it accepts the request and runs compose
in the background. Measured against a live Portainer 2.45, a redeploy returned
success in 68ms while the container did not yet exist. And "settled" is not
"working": a stack whose containers came back and died, or came back
unhealthy, settles just the same. Only replaced AND working is a success.
"""
import pytest

import app.main as main


class FakeClient:
    """Serves a scripted sequence of container listings, one per poll; the
    last one repeats."""

    def __init__(self, listings):
        self.listings = list(listings)
        self.calls = 0

    async def list_containers(self, endpoint_id):
        self.calls += 1
        return self.listings[min(self.calls - 1, len(self.listings) - 1)]


STACK = {"Id": 1, "Name": "dozzle", "EndpointId": 5}


def container(cid, status="Up 2 seconds", state="running", image="sha256:img"):
    return {"Id": cid, "State": state, "Status": status, "ImageID": image,
            "Labels": {"com.docker.compose.project": "dozzle"}}


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(main, "DEPLOY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(main, "DEPLOY_TIMEOUT_SECONDS", 1.0)


async def run(listings):
    client = FakeClient(listings)
    before = main._fingerprint(await main._stack_containers_now(client, STACK))
    return await main._await_deploy(client, STACK, before)


@pytest.mark.asyncio
async def test_waits_for_the_recreate_then_reports_it():
    old, new = [container("old1")], [container("new1", image="sha256:new")]
    outcome = await run([old, old, new])
    assert outcome["ok"] is True
    assert "Redeployed 1 container" in outcome["message"]


@pytest.mark.asyncio
async def test_uptime_text_alone_is_not_a_change():
    """"Up 5 seconds" → "Up 7 seconds" used to count as deploy activity, so
    every deploy took exactly as long as Docker takes to say "About a minute"."""
    ticking = [[container("same", status=f"Up {n} seconds")] for n in range(2, 40, 2)]
    outcome = await run(ticking)
    assert outcome["ok"] is False
    assert "nothing about the stack's containers changed" in outcome["message"]


@pytest.mark.asyncio
async def test_a_deploy_still_running_at_the_timeout_is_not_called_done():
    class NeverSettles:
        calls = 0

        async def list_containers(self, endpoint_id):
            NeverSettles.calls += 1
            return [container(f"c{NeverSettles.calls}")]

    client = NeverSettles()
    before = main._fingerprint(await main._stack_containers_now(client, STACK))
    outcome = await main._await_deploy(client, STACK, before)
    assert outcome["ok"] is False and "not confirmed" in outcome["message"]


@pytest.mark.asyncio
async def test_a_container_that_came_back_dead_is_a_failure():
    old = [container("old1")]
    dead = [container("new1", state="exited", status="Exited (1) 3 seconds ago", image="sha256:new")]
    outcome = await run([old, dead])
    assert outcome["ok"] is False
    assert "not working" in outcome["message"] and "new1" in outcome["message"]


@pytest.mark.asyncio
async def test_unhealthy_is_not_success():
    old = [container("old1")]
    sick = [container("new1", status="Up 9 seconds (unhealthy)", image="sha256:new")]
    outcome = await run([old, sick])
    assert outcome["ok"] is False and "unhealthy" in outcome["message"]


@pytest.mark.asyncio
async def test_a_one_shot_job_that_finished_cleanly_is_fine():
    old = [container("api"), container("backup")]
    new = [container("api2", image="sha256:new"),
           container("backup2", state="exited", status="Exited (0) 2 seconds ago", image="sha256:new")]
    outcome = await run([old, new])
    assert outcome["ok"] is True and "Redeployed 2 containers" in outcome["message"]


@pytest.mark.asyncio
async def test_a_stack_that_settles_empty_is_a_failure():
    outcome = await run([[container("old1")], []])
    assert outcome["ok"] is False and "no containers" in outcome["message"]


@pytest.mark.asyncio
async def test_transient_listing_error_is_ignored_not_read_as_a_change():
    same = [container("c1")]

    class Flaky:
        calls = 0

        async def list_containers(self, endpoint_id):
            Flaky.calls += 1
            if Flaky.calls % 2 == 0:
                raise RuntimeError("proxy hiccup")
            return same

    client = Flaky()
    before = main._fingerprint(await main._stack_containers_now(client, STACK))
    outcome = await main._await_deploy(client, STACK, before)
    assert outcome["ok"] is False and "nothing about the stack's containers changed" in outcome["message"]
