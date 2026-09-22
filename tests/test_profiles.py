"""A service behind a compose profile is declared but never deployed.

Portainer never activates profiles, so such a service has never run and its
image has never been pulled. Restruo read every `image:` in the file, asked the
host about one it had never seen, got a 404, and reported "couldn't check" — a
failure notice for something that is not a failure.
"""
import httpx
import pytest

from app.instances import InstanceRecord
from app.portainer import PortainerClient, profiled_images
from app.registry import RegistryClient
from app.updates import STATUS_NOT_DEPLOYED, STATUS_UNKNOWN, UpdateChecker

QUAERO = """
services:
  db:
    image: pgvector/pgvector:pg17
  api:
    # retired — start only on request
    profiles: ["api"]
    image: ghcr.io/jwapps-app/theology-rag-api:latest
  web:
    image: ghcr.io/jwapps-app/theology-rag-web:${TAG:-latest}
  worker:
    profiles:
      - extras
    image: ${PREFIX}/theology-rag-worker:latest
"""


def test_profiled_services_are_found_with_and_without_interpolation():
    found = profiled_images(QUAERO, {"PREFIX": "ghcr.io/jwapps-app"})
    assert "ghcr.io/jwapps-app/theology-rag-api:latest" in found
    assert "ghcr.io/jwapps-app/theology-rag-worker:latest" in found, "interpolated form"
    assert "${PREFIX}/theology-rag-worker:latest" in found, "raw form, for the fallback path"
    assert not any("theology-rag-web" in i for i in found), "no profile — a normal service"
    assert not any("pgvector" in i for i in found)


@pytest.mark.parametrize("content", ["", "not: [valid", "just a string", "services: 3"])
def test_unparseable_or_odd_files_yield_nothing(content):
    assert profiled_images(content) == set()


@pytest.mark.asyncio
async def test_profiled_image_reads_not_deployed_rather_than_unknown():
    """The exact live case: the api image is not on the host (404), the others are."""
    def portainer(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/stacks":
            return httpx.Response(200, json=[{"Id": 1, "Name": "quaero", "EndpointId": 6, "Env": []}])
        if p == "/api/stacks/1/file":
            return httpx.Response(200, json={"StackFileContent": QUAERO})
        if p == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 6, "Name": "docker-books"}])
        if p.endswith("/containers/json"):
            return httpx.Response(200, json=[])
        if "/images/" in p and "theology-rag-api" in p:
            return httpx.Response(404, json={"message": "No such image: ghcr.io/jwapps-app/theology-rag-api:latest"})
        if "/images/" in p:
            return httpx.Response(200, json={"Id": "sha256:x", "RepoTags": [p.split("/images/")[1].rsplit("/json", 1)[0]],
                                             "RepoDigests": ["x@sha256:" + "a" * 64]})
        return httpx.Response(404, json={"message": "unhandled"})

    def registry(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"docker-content-digest": "sha256:" + "a" * 64})

    client = PortainerClient(
        InstanceRecord(id=5, name="Proxmox", base_url="https://p.test", auth_type="api_key", api_key="k"),
        transport=httpx.MockTransport(portainer))
    checker = UpdateChecker(lambda: [(5, client)], RegistryClient(transport=httpx.MockTransport(registry)),
                            interval_hours=6)
    snapshot = await checker.check_all()
    by_image = {i["image"]: i for i in snapshot["instances"][0]["stacks"][0]["images"]}

    api = by_image["ghcr.io/jwapps-app/theology-rag-api:latest"]
    assert api["status"] == STATUS_NOT_DEPLOYED
    assert "profile" in api["detail"]
    assert STATUS_UNKNOWN not in {i["status"] for i in by_image.values()}
    assert by_image["ghcr.io/jwapps-app/theology-rag-web:latest"]["status"] == "up-to-date"
    assert snapshot["instances"][0]["stacks"][0]["updatesAvailable"] == 0
