"""A second redeploy of the same stack must be refused with a reason.

Portainer deploys one stack at a time and rejects an overlapping request with
a generic "Unable to update stack" — which reads as "the update failed", not
"you asked twice". A redeploy can take a minute while compose waits on a
healthcheck, so clicking again, or from another tab, is easy to do.
"""
import asyncio

import httpx
import pytest

from app.portainer import PortainerClient, PortainerError
from app.instances import InstanceRecord

RECORD = InstanceRecord(id=5, name="Proxmox", base_url="https://p.test:9443",
                        auth_type="api_key", api_key="k")


def test_portainer_details_are_not_discarded():
    """The cause lives in `details`; `message` alone is generic."""
    response = httpx.Response(
        500,
        json={"message": "Unable to update stack",
              "details": "stack is already being deployed"},
        request=httpx.Request("PUT", "https://p.test/api/stacks/42"),
    )
    with pytest.raises(PortainerError) as caught:
        PortainerClient._check(response)
    assert "Unable to update stack" in caught.value.message
    assert "already being deployed" in caught.value.message


def test_message_alone_still_works():
    response = httpx.Response(
        404, json={"message": "Stack not found"},
        request=httpx.Request("GET", "https://p.test/api/stacks/42"),
    )
    with pytest.raises(PortainerError) as caught:
        PortainerClient._check(response)
    assert caught.value.message == "Stack not found"


@pytest.mark.asyncio
async def test_second_update_of_the_same_stack_is_refused():
    from fastapi import HTTPException
    from app.main import _exclusive

    class FakeApp:
        class state:
            in_flight: set = set()

    class FakeRequest:
        app = FakeApp

    started = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with _exclusive(FakeRequest, ("stack", 5, 42)):
            started.set()
            await release.wait()

    task = asyncio.create_task(first())
    await started.wait()

    with pytest.raises(HTTPException) as caught:
        async with _exclusive(FakeRequest, ("stack", 5, 42)):
            pass
    assert caught.value.status_code == 409
    assert "already running" in caught.value.detail

    # A different stack is unaffected — Portainer handles those concurrently.
    async with _exclusive(FakeRequest, ("stack", 5, 43)):
        pass

    release.set()
    await task

    # Once finished, the same stack can be updated again.
    async with _exclusive(FakeRequest, ("stack", 5, 42)):
        pass
