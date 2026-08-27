"""A container is behind when its tag has been re-pulled onto a newer image.

A failed recreate (or any bare `docker pull`) moves the tag — and the digest
reference — onto the new image, leaving the running one with empty RepoTags and
empty RepoDigests. Restruo then saw a nameless image and called it "pinned",
or, having no digest to compare, "local". Both readings hide an update that is
already sitting on the host, which is the wrong direction for a tool whose job
is saying what needs updating.
"""
import httpx
import pytest

from app.instances import InstanceRecord
from app.portainer import PortainerClient, resolve_image_name
from app.registry import RegistryClient
from app.updates import UpdateChecker

OLD_ID = "sha256:" + "43c8bc9bafde".ljust(64, "0")
NEW_ID = "sha256:" + "d63bfe57a106".ljust(64, "0")

CONTAINER = {
    "Id": "3c8646c2b71b", "Names": ["/portainer_agent"],
    "Image": OLD_ID, "ImageID": OLD_ID, "State": "running",
}


def engine():
    """Docker as it really looks after the tag has moved on."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/containers/json"):
            return httpx.Response(200, json=[CONTAINER])
        if "/containers/" in path and path.endswith("/json"):
            # The container still remembers what it was created from.
            return httpx.Response(200, json={"Config": {"Image": "portainer/agent:latest"}})
        if f"/images/{OLD_ID}/json" in path:
            # Stripped of both its tag and its digest by the newer pull.
            return httpx.Response(200, json={"Id": OLD_ID, "RepoTags": [], "RepoDigests": []})
        if "/images/portainer/agent:latest/json" in path:
            return httpx.Response(200, json={
                "Id": NEW_ID, "RepoTags": ["portainer/agent:latest"],
                "RepoDigests": ["portainer/agent@" + NEW_ID],
            })
        return httpx.Response(404, json={"message": "not found"})
    return httpx.MockTransport(handler)


RECORD = InstanceRecord(id=5, name="Proxmox", base_url="https://p.test:9443",
                        auth_type="api_key", api_key="k")


@pytest.mark.asyncio
async def test_image_name_recovered_from_the_container():
    client = PortainerClient(RECORD, transport=engine())
    assert await resolve_image_name(client, 11, CONTAINER) == "portainer/agent:latest"


@pytest.mark.asyncio
async def test_already_pulled_reads_as_update_available_not_local():
    client = PortainerClient(RECORD, transport=engine())
    checker = UpdateChecker(
        lambda: [], RegistryClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(500))), interval_hours=6,
    )
    result = await checker._check_image(client, 11, "portainer/agent:latest", [CONTAINER])

    assert result["status"] == "update-available", result
    assert "already pulled" in result["detail"]
    assert "43c8bc9bafde" in result["detail"]


@pytest.mark.asyncio
async def test_a_genuinely_local_image_is_still_local():
    """Same shape, but nothing newer exists — must not become a false update."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/containers/json"):
            return httpx.Response(200, json=[CONTAINER])
        if "/containers/" in path and path.endswith("/json"):
            return httpx.Response(200, json={"Config": {"Image": "homebuilt/app:latest"}})
        if "/images/" in path:
            # The tag points at the very image the container runs.
            return httpx.Response(200, json={"Id": OLD_ID, "RepoTags": ["homebuilt/app:latest"],
                                             "RepoDigests": []})
        return httpx.Response(404, json={"message": "not found"})

    client = PortainerClient(RECORD, transport=httpx.MockTransport(handler))
    checker = UpdateChecker(
        lambda: [], RegistryClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(500))), interval_hours=6,
    )
    result = await checker._check_image(client, 11, "homebuilt/app:latest", [CONTAINER])
    assert result["status"] == "local", result
