"""Async client for a single Portainer instance.

Implements the calls from the spec (§3) with the compose-vs-git redeploy
branching (§3.7). Field names in request bodies are PascalCase and
case-sensitive per Portainer's API.

Two auth modes: an API token sent as X-API-Key, or username/password exchanged
at /api/auth for a session JWT that is refreshed automatically on expiry.
Secrets are never logged.
"""

import asyncio
import re

import httpx

# Reads (list/get) should fail fast; a redeploy legitimately takes minutes
# because Portainer pulls images before recreating containers.
READ_TIMEOUT = 10.0
REDEPLOY_TIMEOUT = 600.0

STACK_TYPE_NAMES = {1: "swarm", 2: "compose", 3: "kubernetes"}
STACK_STATUS_NAMES = {1: "active", 2: "inactive"}

_IMAGE_RE = re.compile(r"^\s*image\s*:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


class PortainerError(Exception):
    """Raised when a Portainer instance returns an error response.

    Carries Portainer's error body verbatim so bad tokens / RBAC problems are
    debuggable from the dashboard.
    """

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Portainer returned {status_code}: {message}")


def extract_images(stack_file_content: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in _IMAGE_RE.findall(stack_file_content or ""):
        seen.setdefault(match)
    return list(seen)


class PortainerClient:
    def __init__(self, instance, transport: httpx.AsyncBaseTransport | None = None):
        self.instance = instance
        self._auth_type = getattr(instance, "auth_type", "api_key")
        headers = {}
        if self._auth_type == "api_key":
            headers["X-API-Key"] = instance.api_key
        kwargs = {} if transport is None else {"transport": transport}
        self._client = httpx.AsyncClient(
            base_url=instance.base_url,
            headers=headers,
            verify=instance.verify_tls,
            timeout=READ_TIMEOUT,
            **kwargs,
        )
        self._jwt: str | None = None
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        response = await self._client.post(
            "/api/auth",
            json={"Username": self.instance.username, "Password": self.instance.password},
        )
        self._check(response)
        self._jwt = response.json().get("jwt")

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self._auth_type != "credentials":
            return await self._client.request(method, url, **kwargs)

        if self._jwt is None:
            async with self._auth_lock:
                if self._jwt is None:
                    await self._login()
        headers = kwargs.setdefault("headers", {})
        headers["Authorization"] = f"Bearer {self._jwt}"
        response = await self._client.request(method, url, **kwargs)
        if response.status_code == 401:
            # Session JWT expired — log in again and retry once.
            async with self._auth_lock:
                await self._login()
            headers["Authorization"] = f"Bearer {self._jwt}"
            response = await self._client.request(method, url, **kwargs)
        return response

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.is_error:
            try:
                body = response.json()
                message = body.get("message") or body.get("details") or response.text
            except ValueError:
                message = response.text
            raise PortainerError(response.status_code, message)

    async def list_endpoints(self) -> list[dict]:
        response = await self._request("GET", "/api/endpoints")
        self._check(response)
        return response.json()

    async def list_stacks(self) -> list[dict]:
        response = await self._request("GET", "/api/stacks")
        self._check(response)
        return response.json()

    async def get_stack_file(self, stack_id: int) -> str:
        response = await self._request("GET", f"/api/stacks/{stack_id}/file")
        self._check(response)
        return response.json().get("StackFileContent", "")

    async def get_image_info(self, endpoint_id: int, image: str) -> dict:
        """Inspect an image on the environment's Docker engine via Portainer's
        docker proxy. Used by update checks to read local RepoDigests."""
        response = await self._request(
            "GET", f"/api/endpoints/{endpoint_id}/docker/images/{image}/json"
        )
        self._check(response)
        return response.json()

    async def redeploy_compose(
        self,
        stack_id: int,
        endpoint_id: int,
        stack_file_content: str,
        env: list[dict],
        prune: bool = False,
    ) -> dict:
        response = await self._request(
            "PUT",
            f"/api/stacks/{stack_id}",
            params={"endpointId": endpoint_id},
            json={
                "StackFileContent": stack_file_content,
                "Env": env or [],
                "PullImage": True,
                "Prune": prune,
            },
            timeout=REDEPLOY_TIMEOUT,
        )
        self._check(response)
        return response.json()

    async def redeploy_git(
        self,
        stack_id: int,
        endpoint_id: int,
        env: list[dict],
        prune: bool = False,
    ) -> dict:
        response = await self._request(
            "PUT",
            f"/api/stacks/{stack_id}/git/redeploy",
            params={"endpointId": endpoint_id},
            json={
                "Env": env or [],
                "Prune": prune,
                "RepullImageAndRedeploy": True,
            },
            timeout=REDEPLOY_TIMEOUT,
        )
        self._check(response)
        return response.json()

    async def update_stack(self, stack: dict) -> dict:
        """Repull + redeploy one stack, branching on GitConfig per spec §3.7.

        `stack` is the raw stack object from list_stacks — Env and EndpointId
        are re-sent from it so redeploys never wipe env vars or hit the wrong
        environment.
        """
        stack_id = stack["Id"]
        endpoint_id = stack["EndpointId"]
        env = stack.get("Env") or []

        if stack.get("GitConfig"):
            return await self.redeploy_git(stack_id, endpoint_id, env)

        # Compose/editor path: fetch the file fresh right before the PUT so we
        # never redeploy stale content.
        stack_file_content = await self.get_stack_file(stack_id)
        return await self.redeploy_compose(stack_id, endpoint_id, stack_file_content, env)


def normalize_stack(stack: dict, images: list[str]) -> dict:
    """Shape a raw Portainer stack object for the dashboard API."""
    return {
        "id": stack["Id"],
        "name": stack.get("Name", ""),
        "endpointId": stack.get("EndpointId"),
        "type": STACK_TYPE_NAMES.get(stack.get("Type"), f"unknown({stack.get('Type')})"),
        "git": bool(stack.get("GitConfig")),
        "status": STACK_STATUS_NAMES.get(stack.get("Status"), "unknown"),
        "images": images,
        "updatedAt": stack.get("UpdateDate") or stack.get("CreationDate"),
    }
