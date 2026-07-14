"""Unit tests for the compose-vs-git redeploy branching (spec §3.7) against a
mocked Portainer API."""

import json

import httpx
import pytest

from app.config import InstanceConfig
from app.portainer import PortainerClient, PortainerError, extract_images, normalize_stack

INSTANCE = InstanceConfig(
    name="Test", base_url="https://portainer.test:9443", api_key="ptr_test", verify_tls=True
)

COMPOSE_YAML = "services:\n  web:\n    image: nginx:1.27\n  db:\n    image: mariadb:11\n"

GIT_STACK = {
    "Id": 5,
    "Name": "git-stack",
    "EndpointId": 2,
    "Type": 2,
    "GitConfig": {"URL": "https://github.com/x/y", "ReferenceName": "refs/heads/main"},
    "Env": [{"name": "IMAGE_TAG", "value": "latest"}],
}

COMPOSE_STACK = {
    "Id": 7,
    "Name": "compose-stack",
    "EndpointId": 3,
    "Type": 2,
    "GitConfig": None,
    "Env": [{"name": "FOO", "value": "bar"}],
}


def make_client(handler) -> tuple[PortainerClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = PortainerClient(INSTANCE, transport=httpx.MockTransport(recording_handler))
    return client, requests


async def test_git_stack_uses_git_redeploy_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/stacks/5/git/redeploy"
        return httpx.Response(200, json={"Id": 5})

    client, requests = make_client(handler)
    await client.update_stack(GIT_STACK)
    await client.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.url.params["endpointId"] == "2"
    assert request.headers["X-API-Key"] == "ptr_test"
    body = json.loads(request.content)
    assert body["RepullImageAndRedeploy"] is True
    assert body["Prune"] is False
    assert body["Env"] == [{"name": "IMAGE_TAG", "value": "latest"}]
    assert "StackFileContent" not in body


async def test_compose_stack_fetches_file_then_puts_with_pullimage():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/stacks/7/file":
            return httpx.Response(200, json={"StackFileContent": COMPOSE_YAML})
        if request.method == "PUT" and request.url.path == "/api/stacks/7":
            return httpx.Response(200, json={"Id": 7})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client, requests = make_client(handler)
    await client.update_stack(COMPOSE_STACK)
    await client.aclose()

    # File is fetched fresh right before the PUT.
    assert [r.method for r in requests] == ["GET", "PUT"]
    put = requests[1]
    assert put.url.params["endpointId"] == "3"
    body = json.loads(put.content)
    assert body["PullImage"] is True
    assert body["Prune"] is False
    assert body["StackFileContent"] == COMPOSE_YAML
    assert body["Env"] == [{"name": "FOO", "value": "bar"}]
    assert "RepullImageAndRedeploy" not in body


async def test_branching_uses_gitconfig_not_type():
    # A swarm-typed stack (Type: 1) with GitConfig must still take the git path.
    swarm_git = {**GIT_STACK, "Type": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/stacks/5/git/redeploy"
        return httpx.Response(200, json={"Id": 5})

    client, requests = make_client(handler)
    await client.update_stack(swarm_git)
    await client.aclose()
    assert len(requests) == 1


async def test_missing_env_defaults_to_empty_list():
    stack = {**GIT_STACK, "Env": None}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Id": 5})

    client, requests = make_client(handler)
    await client.update_stack(stack)
    await client.aclose()
    assert json.loads(requests[0].content)["Env"] == []


async def test_recreate_container_uses_portainer_recreate_action():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/docker/2/containers/abc123/recreate"
        assert json.loads(request.content) == {"PullImage": True}
        return httpx.Response(200, json={"Id": "def456"})

    client, requests = make_client(handler)
    await client.recreate_container(2, "abc123")
    await client.aclose()
    assert len(requests) == 1


async def test_prune_images_removes_all_unused_not_just_dangling():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/endpoints/2/docker/images/prune"
        assert json.loads(request.url.params["filters"]) == {"dangling": ["false"]}
        return httpx.Response(200, json={"ImagesDeleted": [{}, {}], "SpaceReclaimed": 123})

    client, _ = make_client(handler)
    pruned = await client.prune_images(2)
    await client.aclose()
    assert pruned["SpaceReclaimed"] == 123


async def test_prune_volumes_includes_named_with_fallback():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/endpoints/2/docker/volumes/prune"
        calls.append(request.url.params.get("filters"))
        if request.url.params.get("filters"):
            # Older engine: rejects the "all" filter.
            return httpx.Response(400, json={"message": "invalid filter 'all'"})
        return httpx.Response(200, json={"VolumesDeleted": ["v1"], "SpaceReclaimed": 55})

    client, _ = make_client(handler)
    pruned = await client.prune_volumes(2)
    await client.aclose()
    assert calls == ['{"all": ["true"]}', None]
    assert pruned["VolumesDeleted"] == ["v1"]


async def test_prune_networks():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/endpoints/2/docker/networks/prune"
        return httpx.Response(200, json={"NetworksDeleted": ["a", "b"]})

    client, _ = make_client(handler)
    pruned = await client.prune_networks(2)
    await client.aclose()
    assert len(pruned["NetworksDeleted"]) == 2


async def test_portainer_error_surfaces_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "access denied to resource"})

    client, _ = make_client(handler)
    with pytest.raises(PortainerError) as excinfo:
        await client.update_stack(GIT_STACK)
    await client.aclose()
    assert excinfo.value.status_code == 403
    assert "access denied" in excinfo.value.message


def test_extract_images():
    yaml = (
        "services:\n"
        "  web:\n"
        "    image: nginx:1.27\n"
        "  app:\n"
        "    image: 'ghcr.io/acme/app:v2'  # pinned\n"
        "  dupe:\n"
        "    image: nginx:1.27\n"
    )
    assert extract_images(yaml) == ["nginx:1.27", "ghcr.io/acme/app:v2"]
    assert extract_images("") == []


def test_normalize_stack():
    normalized = normalize_stack(
        {**COMPOSE_STACK, "Status": 1, "UpdateDate": 1751400000}, ["nginx:1.27"]
    )
    assert normalized == {
        "id": 7,
        "name": "compose-stack",
        "endpointId": 3,
        "type": "compose",
        "git": False,
        "status": "active",
        "images": ["nginx:1.27"],
        "updatedAt": 1751400000,
    }
