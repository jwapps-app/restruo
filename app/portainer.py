"""Async client for a single Portainer instance.

Implements the calls from the spec (§3) with the compose-vs-git redeploy
branching (§3.7). Field names in request bodies are PascalCase and
case-sensitive per Portainer's API.

Two auth modes: an API token sent as X-API-Key, or username/password exchanged
at /api/auth for a session JWT that is refreshed automatically on expiry.
Secrets are never logged.
"""

import asyncio
import json
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
        self._csrf: str | None = None
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

    async def _fetch_csrf(self) -> None:
        # Portainer 2.20.2+ requires an X-CSRF-Token on mutating requests made
        # with a session JWT (API keys are exempt). The token is issued via a
        # response header on any GET, paired with a cookie the client jar keeps.
        response = await self._client.get("/")
        self._csrf = response.headers.get("X-CSRF-Token") or self._csrf

    @staticmethod
    def _is_csrf_error(response: httpx.Response) -> bool:
        return response.status_code == 403 and "csrf" in response.text.lower()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self._auth_type != "credentials":
            return await self._client.request(method, url, **kwargs)

        # A Portainer restart invalidates the JWT, the CSRF token, AND the CSRF
        # cookie at once, and they can only be re-established in sequence. Run
        # the request as a self-healing loop: on each failure, reset whichever
        # piece went stale and try again with the rest rebuilt.
        mutating = method.upper() not in ("GET", "HEAD", "OPTIONS")
        response: httpx.Response | None = None
        for _ in range(3):
            if self._jwt is None:
                async with self._auth_lock:
                    if self._jwt is None:
                        await self._login()
            if mutating and self._csrf is None:
                async with self._auth_lock:
                    if self._csrf is None:
                        await self._fetch_csrf()

            headers = kwargs.setdefault("headers", {})
            headers["Authorization"] = f"Bearer {self._jwt}"
            if mutating:
                if self._csrf:
                    headers["X-CSRF-Token"] = self._csrf
                # Over HTTPS, Portainer's CSRF layer additionally requires a
                # same-origin Referer ("Forbidden - referer not supplied").
                headers["Referer"] = f"{self.instance.base_url}/"
                headers["Origin"] = self.instance.base_url
            response = await self._client.request(method, url, **kwargs)

            if response.status_code == 401:
                self._jwt = None  # expired session — re-login on next pass
                continue
            if self._is_csrf_error(response):
                # Stale CSRF state — drop the token AND the cookie jar so the
                # next pass starts a fresh CSRF handshake.
                self._csrf = None
                self._client.cookies.clear()
                continue
            return response
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

    async def get_stack(self, stack_id: int) -> dict:
        response = await self._request("GET", f"/api/stacks/{stack_id}")
        self._check(response)
        return response.json()

    async def get_stack_file(self, stack_id: int) -> str:
        response = await self._request("GET", f"/api/stacks/{stack_id}/file")
        self._check(response)
        return response.json().get("StackFileContent", "")

    async def list_containers(self, endpoint_id: int) -> list[dict]:
        """All containers (running or not) on the environment's Docker engine."""
        response = await self._request(
            "GET",
            f"/api/endpoints/{endpoint_id}/docker/containers/json",
            params={"all": "1"},
        )
        self._check(response)
        return response.json()

    async def get_image_info(self, endpoint_id: int, image: str) -> dict:
        """Inspect an image on the environment's Docker engine via Portainer's
        docker proxy. Used by update checks to read local RepoDigests."""
        response = await self._request(
            "GET", f"/api/endpoints/{endpoint_id}/docker/images/{image}/json"
        )
        self._check(response)
        return response.json()

    async def prune_images(self, endpoint_id: int) -> dict:
        """Remove ALL unused images (dangling=false), not just untagged layers —
        this is what reclaims space from superseded :latest pulls."""
        response = await self._request(
            "POST",
            f"/api/endpoints/{endpoint_id}/docker/images/prune",
            params={"filters": json.dumps({"dangling": ["false"]})},
            timeout=REDEPLOY_TIMEOUT,
        )
        self._check(response)
        return response.json()

    async def prune_networks(self, endpoint_id: int) -> dict:
        response = await self._request(
            "POST",
            f"/api/endpoints/{endpoint_id}/docker/networks/prune",
            timeout=REDEPLOY_TIMEOUT,
        )
        self._check(response)
        return response.json()

    async def prune_volumes(self, endpoint_id: int) -> dict:
        """Remove ALL volumes no container references — named ones included.
        Destructive by nature; callers must gate this behind explicit consent.
        Older engines reject the all filter, where the default prune (anonymous
        volumes only) is the best available."""
        try:
            response = await self._request(
                "POST",
                f"/api/endpoints/{endpoint_id}/docker/volumes/prune",
                params={"filters": json.dumps({"all": ["true"]})},
                timeout=REDEPLOY_TIMEOUT,
            )
            self._check(response)
        except PortainerError as exc:
            if exc.status_code != 400:
                raise
            response = await self._request(
                "POST",
                f"/api/endpoints/{endpoint_id}/docker/volumes/prune",
                timeout=REDEPLOY_TIMEOUT,
            )
            self._check(response)
        return response.json()

    async def recreate_container(self, endpoint_id: int, container_id: int | str) -> dict:
        """Portainer's own recreate action: pulls the image fresh and recreates
        the container with its existing configuration (same as the UI's
        Recreate button with 're-pull image' enabled)."""
        response = await self._request(
            "POST",
            f"/api/docker/{endpoint_id}/containers/{container_id}/recreate",
            json={"PullImage": True},
            timeout=REDEPLOY_TIMEOUT,
        )
        self._check(response)
        try:
            return response.json()
        except ValueError:
            return {}

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


async def resolve_image_name(
    client: PortainerClient, endpoint_id: int, container: dict
) -> str:
    """Containers whose image tag was re-pulled elsewhere report a bare sha256
    digest as their image. Resolve it back to a repository name via the image
    metadata so display, update checks, and guards keep working."""
    image = container.get("Image") or ""
    if not image.startswith("sha256:"):
        return image
    try:
        info = await client.get_image_info(endpoint_id, container.get("ImageID") or image)
        tags = info.get("RepoTags") or []
        digests = info.get("RepoDigests") or []
        return (tags or digests or [image])[0]
    except Exception:
        return image


def standalone_containers(containers: list[dict], stack_names: set[str]) -> list[dict]:
    """Containers that don't belong to any Portainer stack on this instance."""
    out = []
    for container in containers:
        labels = container.get("Labels") or {}
        project = labels.get("com.docker.compose.project") or labels.get(
            "com.docker.stack.namespace"
        )
        if project and project in stack_names:
            continue
        out.append(container)
    return out


def stack_containers(stack: dict, containers: list[dict]) -> list[dict]:
    """Containers belonging to one Portainer stack (compose or swarm)."""
    name = stack.get("Name", "")
    out = []
    for container in containers:
        labels = container.get("Labels") or {}
        if labels.get("com.docker.compose.project") == name or \
                labels.get("com.docker.stack.namespace") == name:
            out.append(container)
    return out


def container_is_down(container: dict) -> bool:
    """Not running, or running but failing its healthcheck."""
    state = (container.get("State") or "").lower()
    status = container.get("Status") or ""
    return state != "running" or "(unhealthy)" in status


def container_name(container: dict) -> str:
    names = container.get("Names") or []
    return names[0].lstrip("/") if names else container.get("Id", "")[:12]


def normalize_container(container: dict, endpoint_id: int) -> dict:
    return {
        "id": container.get("Id", ""),
        "name": container_name(container),
        "image": container.get("Image", ""),
        "state": container.get("State", ""),
        "statusText": container.get("Status", ""),
        "down": container_is_down(container),
        "endpointId": endpoint_id,
    }


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
