"""A stack update must report the deploy, not its acceptance.

Portainer's stack API is asynchronous: it accepts the request and runs compose
in the background. Measured against a live Portainer 2.45, a redeploy returned
`{"ok":true,"message":"Repulled and redeployed."}` in 68ms while the container
did not yet exist — it appeared three seconds later. So the tick, the duration
and the cleared badge all described a deploy that had not happened, and with a
slow pull the UI said "done" a minute early.
"""
import asyncio

import pytest

import app.main as main


class FakeClient:
    """Serves a scripted sequence of container listings, one per poll."""

    def __init__(self, listings):
        self.listings = list(listings)
        self.calls = 0

    async def list_containers(self, endpoint_id):
        self.calls += 1
        index = min(self.calls - 1, len(self.listings) - 1)
        return self.listings[index]


STACK = {"Id": 1, "Name": "dozzle", "EndpointId": 5}


def container(cid, status="Up 2 seconds"):
    return {"Id": cid, "State": "running", "Status": status,
            "Labels": {"com.docker.compose.project": "dozzle"}}


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(main, "DEPLOY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(main, "DEPLOY_TIMEOUT_SECONDS", 1.0)


@pytest.mark.asyncio
async def test_waits_for_the_recreate_then_reports_it():
    old, new = [container("old1")], [container("new1")]
    # Accepted, container still the old one, then replaced, then steady.
    client = FakeClient([old, old, new, new, new, new])
    before = await main._stack_fingerprint(client, STACK)

    message = await main._await_deploy(client, STACK, before)

    assert "Redeployed 1 container" in message


@pytest.mark.asyncio
async def test_a_no_op_says_so_rather_than_claiming_a_redeploy():
    same = [container("same1", status="Up 3 hours")]
    client = FakeClient([same])
    before = await main._stack_fingerprint(client, STACK)

    message = await main._await_deploy(client, STACK, before)

    assert "nothing changed" in message
    assert "Redeployed" not in message


@pytest.mark.asyncio
async def test_a_deploy_still_running_at_the_timeout_is_not_called_done():
    """Never claim success for something still in flight."""

    class NeverSettles:
        calls = 0

        async def list_containers(self, endpoint_id):
            NeverSettles.calls += 1
            # A different container every poll: the deploy is still churning.
            return [container(f"c{NeverSettles.calls}")]

    client = NeverSettles()
    before = await main._stack_fingerprint(client, STACK)

    message = await main._await_deploy(client, STACK, before)

    assert "Still deploying" in message
    assert "Redeployed" not in message


@pytest.mark.asyncio
async def test_multi_container_stack_counts_what_was_replaced():
    old = [container("a"), container("b"), container("c")]
    new = [container("a2"), container("b2"), container("c")]
    client = FakeClient([old, old, new, new, new, new])
    before = await main._stack_fingerprint(client, STACK)

    message = await main._await_deploy(client, STACK, before)

    assert "Redeployed 2 containers" in message
