"""Tests for update checking: image-ref parsing, registry digest fetch, and
the latest-only check policy against mocked Portainer + registry APIs."""

import httpx
import pytest

from app.config import InstanceConfig
from app.notifiers import Notifier
from app.portainer import PortainerClient
from app.registry import RegistryClient, RegistryError, parse_image_ref
from app.updates import UpdateChecker

INSTANCE = InstanceConfig(
    name="Test", base_url="https://portainer.test:9443", api_key="ptr_test", verify_tls=True
)

OLD_DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64


# --- parse_image_ref -------------------------------------------------------

@pytest.mark.parametrize(
    "raw,registry,repository,tag,tracks_latest",
    [
        ("nginx", "docker.io", "library/nginx", "latest", True),
        ("nginx:latest", "docker.io", "library/nginx", "latest", True),
        ("mariadb:11", "docker.io", "library/mariadb", "11", False),
        ("acme/app", "docker.io", "acme/app", "latest", True),
        ("ghcr.io/acme/app:latest", "ghcr.io", "acme/app", "latest", True),
        ("lscr.io/linuxserver/plex:1.2.3", "lscr.io", "linuxserver/plex", "1.2.3", False),
        ("localhost:5000/thing", "localhost:5000", "thing", "latest", True),
    ],
)
def test_parse_image_ref(raw, registry, repository, tag, tracks_latest):
    ref = parse_image_ref(raw)
    assert ref.registry == registry
    assert ref.repository == repository
    assert ref.tag == tag
    assert ref.tracks_latest is tracks_latest


def test_parse_image_ref_digest_pin_never_tracks_latest():
    ref = parse_image_ref("nginx:latest@" + OLD_DIGEST)
    assert ref.pinned_digest is True
    assert ref.tracks_latest is False


def test_parse_image_ref_rejects_interpolation_and_empty():
    assert parse_image_ref("myapp:${TAG}") is None
    assert parse_image_ref("") is None


# --- RegistryClient --------------------------------------------------------

def registry_transport(digest: str, require_token: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.registry.test":
            return httpx.Response(200, json={"token": "tok123"})
        assert request.method == "HEAD"
        if require_token and request.headers.get("Authorization") != "Bearer tok123":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.registry.test/token",'
                    'service="registry.test"'
                },
            )
        return httpx.Response(200, headers={"Docker-Content-Digest": digest})

    return httpx.MockTransport(handler)


async def test_registry_token_flow_and_digest():
    client = RegistryClient(transport=registry_transport(NEW_DIGEST))
    ref = parse_image_ref("registry.test/acme/app:latest")
    assert await client.get_remote_digest(ref) == NEW_DIGEST
    # Second call reuses the cached token (handler rejects missing tokens).
    assert await client.get_remote_digest(ref) == NEW_DIGEST
    await client.aclose()


async def test_registry_error_on_missing_manifest():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = RegistryClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RegistryError):
        await client.get_remote_digest(parse_image_ref("registry.test/gone:latest"))
    await client.aclose()


def test_docker_hub_uses_registry_1_host():
    assert RegistryClient._api_host(parse_image_ref("nginx")) == "registry-1.docker.io"
    assert RegistryClient._api_host(parse_image_ref("ghcr.io/a/b")) == "ghcr.io"


# --- UpdateChecker ---------------------------------------------------------

COMPOSE_YAML = (
    "services:\n"
    "  web:\n"
    "    image: registry.test/acme/web:latest\n"
    "  db:\n"
    "    image: registry.test/acme/db:11\n"
)

STACK = {"Id": 1, "Name": "mystack", "EndpointId": 2, "Type": 2, "GitConfig": None, "Env": []}


def portainer_transport(local_digest: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/stacks":
            return httpx.Response(200, json=[STACK])
        if path == "/api/stacks/1/file":
            return httpx.Response(200, json={"StackFileContent": COMPOSE_YAML})
        if path.startswith("/api/endpoints/2/docker/images/") and path.endswith("/json"):
            return httpx.Response(
                200, json={"RepoDigests": [f"registry.test/acme/web@{local_digest}"]}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


class RecordingNotifier(Notifier):
    def __init__(self):
        self.batches = []

    async def send(self, events):
        self.batches.append(events)


async def run_check(local_digest: str, remote_digest: str, notifier=None):
    portainer = PortainerClient(INSTANCE, transport=portainer_transport(local_digest))
    registry = RegistryClient(transport=registry_transport(remote_digest, require_token=False))
    checker = UpdateChecker(
        lambda: [(0, portainer)], registry, interval_hours=6,
        notifiers=[notifier] if notifier else [],
    )
    snapshot = await checker.check_all()
    return checker, snapshot, portainer, registry


async def test_latest_image_with_new_digest_is_update_available():
    notifier = RecordingNotifier()
    checker, snapshot, portainer, registry = await run_check(OLD_DIGEST, NEW_DIGEST, notifier)
    stack = snapshot["instances"][0]["stacks"][0]
    by_image = {img["image"]: img["status"] for img in stack["images"]}
    assert by_image["registry.test/acme/web:latest"] == "update-available"
    assert by_image["registry.test/acme/db:11"] == "pinned"  # never checked
    assert stack["updatesAvailable"] == 1
    assert snapshot["checkedAt"] is not None
    # Notified once with the one new finding.
    assert len(notifier.batches) == 1
    assert notifier.batches[0][0].image == "registry.test/acme/web:latest"

    # A second check finds the same thing — no duplicate notification.
    await checker.check_all()
    assert len(notifier.batches) == 1
    await portainer.aclose()
    await registry.aclose()


async def test_matching_digest_is_up_to_date():
    notifier = RecordingNotifier()
    _, snapshot, portainer, registry = await run_check(NEW_DIGEST, NEW_DIGEST, notifier)
    stack = snapshot["instances"][0]["stacks"][0]
    by_image = {img["image"]: img["status"] for img in stack["images"]}
    assert by_image["registry.test/acme/web:latest"] == "up-to-date"
    assert stack["updatesAvailable"] == 0
    assert notifier.batches == []
    await portainer.aclose()
    await registry.aclose()


def container_aware_transport(running_digest: str, tag_digest: str) -> httpx.MockTransport:
    """Mock where the running container's image differs from what the local tag
    points at (something re-pulled the tag but the container wasn't recreated)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/stacks":
            return httpx.Response(200, json=[STACK])
        if path == "/api/stacks/1/file":
            return httpx.Response(200, json={"StackFileContent": COMPOSE_YAML})
        if path == "/api/endpoints/2/docker/containers/json":
            return httpx.Response(200, json=[{
                "Image": "registry.test/acme/web:latest",
                "ImageID": "sha256:running-image-id",
                "Labels": {"com.docker.compose.project": "mystack"},
            }])
        if path == "/api/endpoints/2/docker/images/sha256:running-image-id/json":
            return httpx.Response(
                200, json={"RepoDigests": [f"registry.test/acme/web@{running_digest}"]}
            )
        if path.startswith("/api/endpoints/2/docker/images/") and path.endswith("/json"):
            return httpx.Response(
                200, json={"RepoDigests": [f"registry.test/acme/web@{tag_digest}"]}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def test_compares_running_container_not_local_tag():
    # The tag was already re-pulled to NEW, but the container still runs OLD:
    # an update IS available (this is what container-watchers like WUD report).
    portainer = PortainerClient(
        INSTANCE, transport=container_aware_transport(OLD_DIGEST, NEW_DIGEST)
    )
    registry = RegistryClient(transport=registry_transport(NEW_DIGEST, require_token=False))
    checker = UpdateChecker(lambda: [(0, portainer)], registry, interval_hours=6)
    snapshot = await checker.check_all()
    by_image = {
        img["image"]: img["status"]
        for img in snapshot["instances"][0]["stacks"][0]["images"]
    }
    assert by_image["registry.test/acme/web:latest"] == "update-available"
    await portainer.aclose()
    await registry.aclose()


def standalone_transport(running_digest: str) -> httpx.MockTransport:
    """Instance with no stacks — just one standalone container tracking :latest."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/stacks":
            return httpx.Response(200, json=[])
        if path == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 2}])
        if path == "/api/endpoints/2/docker/containers/json":
            return httpx.Response(200, json=[{
                "Id": "ctr-1",
                "Names": ["/adguard"],
                "Image": "registry.test/acme/web:latest",
                "ImageID": "sha256:running-image-id",
                "State": "running",
                "Labels": {},
            }])
        if path == "/api/endpoints/2/docker/images/sha256:running-image-id/json":
            return httpx.Response(
                200, json={"RepoDigests": [f"registry.test/acme/web@{running_digest}"]}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def test_standalone_container_checked_and_flagged():
    portainer = PortainerClient(INSTANCE, transport=standalone_transport(OLD_DIGEST))
    registry = RegistryClient(transport=registry_transport(NEW_DIGEST, require_token=False))
    notifier = RecordingNotifier()
    checker = UpdateChecker(
        lambda: [(0, portainer)], registry, interval_hours=6, notifiers=[notifier]
    )
    snapshot = await checker.check_all()
    containers = snapshot["instances"][0]["containers"]
    assert len(containers) == 1
    assert containers[0]["name"] == "adguard"
    assert containers[0]["status"] == "update-available"
    assert notifier.batches[0][0].stack_name == "adguard"
    await portainer.aclose()
    await registry.aclose()


async def test_running_container_current_is_up_to_date():
    portainer = PortainerClient(
        INSTANCE, transport=container_aware_transport(NEW_DIGEST, NEW_DIGEST)
    )
    registry = RegistryClient(transport=registry_transport(NEW_DIGEST, require_token=False))
    checker = UpdateChecker(lambda: [(0, portainer)], registry, interval_hours=6)
    snapshot = await checker.check_all()
    by_image = {
        img["image"]: img["status"]
        for img in snapshot["instances"][0]["stacks"][0]["images"]
    }
    assert by_image["registry.test/acme/web:latest"] == "up-to-date"
    await portainer.aclose()
    await registry.aclose()
