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


# ${VAR}, ${VAR:-default}, ${VAR-default}, ${VAR:?msg}, ${VAR?msg}, $VAR
_INTERPOLATE_RE = re.compile(
    r"\$\{([A-Za-z_]\w*)(?::?[-?]([^}]*))?\}|\$([A-Za-z_]\w*)"
)


def stack_env(stack: dict) -> dict[str, str]:
    """The stack's environment variables, as compose would see them."""
    env = {}
    for item in stack.get("Env") or []:
        name = item.get("name")
        if name:
            env[name] = item.get("value") or ""
    return env


def interpolate(value: str, env: dict[str, str]) -> str | None:
    """Resolve compose-style variables in an image reference.

    Stack files routinely read `${IMAGE_PREFIX}-backend:${IMAGE_TAG:-latest}`;
    the raw file alone says nothing about what is actually running. Returns None
    if a variable has neither a value nor a default — substituting empty text
    would silently produce a nonsense reference like ":latest".
    """
    unresolved = False

    def replace(match: re.Match) -> str:
        nonlocal unresolved
        name = match.group(1) or match.group(3)
        default = match.group(2)
        if env.get(name):
            return env[name]
        if default is not None:
            return default
        unresolved = True
        return ""

    resolved = _INTERPOLATE_RE.sub(replace, value or "")
    return None if unresolved else resolved


def stack_images(stack: dict, file_content: str, containers: list[dict]) -> list[str]:
    """Images a stack uses, resolved as far as we can manage.

    Compose variables are substituted from the stack's own environment; if any
    are still unresolved (defined outside Portainer, say) fall back to what the
    stack's containers are actually running, which is the ground truth anyway.
    """
    env = stack_env(stack)
    declared = extract_images(file_content)
    images = [interpolate(image, env) for image in declared]
    if all(image is not None for image in images):
        return images

    resolved = [image for image in images if image is not None]
    for container in containers:
        image = container.get("Image") or ""
        if image and image not in resolved:
            resolved.append(image)
    # Nothing to fall back on (a stopped stack, say) — keep the raw references
    # so the row still shows something recognisable.
    return resolved or declared


class PortainerClient:
    def __init__(self, instance, transport: httpx.AsyncBaseTransport | None = None):
        self.instance = instance
        self._auth_type = getattr(instance, "auth_type", "api_key")
        self._headers = {}
        if self._auth_type == "api_key":
            self._headers["X-API-Key"] = instance.api_key
        self._injected_transport = transport
        self._client = self._new_client()
        self._jwt: str | None = None
        self._csrf: str | None = None
        self._logged_in = False
        self._auth_lock = asyncio.Lock()

    def _new_client(self) -> httpx.AsyncClient:
        transport = self._injected_transport
        if transport is None:
            # retries covers connect-level failures — after a machine reboots,
            # the first attempt can land on a socket that died silently.
            transport = httpx.AsyncHTTPTransport(
                verify=self.instance.verify_tls, retries=2
            )
        return httpx.AsyncClient(
            base_url=self.instance.base_url,
            headers=self._headers,
            timeout=READ_TIMEOUT,
            transport=transport,
        )

    async def reconnect(self) -> None:
        """Throw away the connection pool and the session.

        When the machine (or its Docker VM) goes away and comes back, the pool
        can hold sockets that are dead but not closed: every request picks one,
        waits out the timeout and fails, so the instance stays 'unreachable'
        long after it is back. Rebuilding is what re-saving the instance used to
        do by hand.
        """
        old = self._client
        self._client = self._new_client()
        self._jwt = None
        self._csrf = None
        self._logged_in = False
        try:
            await old.aclose()
        except Exception:
            pass

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TransportError:
            # Connection-level failure: rebuild the pool and give it one more
            # go, so a machine that just came back recovers on this poll rather
            # than needing the instance re-saved.
            await self.reconnect()
            return await self._client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _harvest_csrf(self, response: httpx.Response) -> None:
        """Portainer 2.20.2+ requires an X-CSRF-Token on mutating requests made
        with a session (API keys are exempt). It issues one on every
        AUTHENTICATED /api response — unauthenticated requests and the SPA page
        return the header empty — so take the token from the calls we already
        make instead of asking for it separately."""
        token = response.headers.get("X-CSRF-Token")
        if token:
            self._csrf = token

    def _origin_headers(self) -> dict[str, str]:
        """Portainer's CSRF layer rejects state-changing requests that lack a
        same-origin Referer ("Forbidden - referer not supplied")."""
        return {
            "Referer": f"{self.instance.base_url}/",
            "Origin": self.instance.base_url,
        }

    async def _login(self) -> None:
        # /api/auth is itself state-changing, so it needs the same-origin
        # headers every other mutating call sends. Without them a Portainer
        # that has just been recreated refuses every login attempt, and the
        # instance stays unreachable until its record is re-saved.
        response = await self._send(
            "POST", "/api/auth",
            headers=self._origin_headers(),
            json={"Username": self.instance.username, "Password": self.instance.password},
        )
        self._check(response)
        try:
            # Recent Portainer returns the session as a cookie and may leave the
            # body's jwt empty; the cookie jar carries it either way.
            self._jwt = response.json().get("jwt") or None
        except ValueError:
            self._jwt = None
        self._logged_in = True
        self._harvest_csrf(response)

    async def _fetch_csrf(self) -> None:
        """Make an authenticated GET purely to obtain a token, for the rare case
        where a mutating call is the first thing this client does."""
        headers = {"Authorization": f"Bearer {self._jwt}"} if self._jwt else {}
        for path in ("/api/endpoints", "/api/system/status"):
            try:
                response = await self._send("GET", path, headers=headers)
            except Exception:
                continue
            self._harvest_csrf(response)
            if self._csrf:
                return

    @staticmethod
    def _is_csrf_error(response: httpx.Response) -> bool:
        return response.status_code == 403 and "csrf" in response.text.lower()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self._auth_type != "credentials":
            return await self._send(method, url, **kwargs)

        # A Portainer restart invalidates the JWT, the CSRF token, AND the CSRF
        # cookie at once, and they can only be re-established in sequence. Run
        # the request as a self-healing loop: on each failure, reset whichever
        # piece went stale and try again with the rest rebuilt.
        mutating = method.upper() not in ("GET", "HEAD", "OPTIONS")
        response: httpx.Response | None = None
        csrf_failures = 0
        for _ in range(3):
            if not self._logged_in:
                async with self._auth_lock:
                    if not self._logged_in:
                        await self._login()
            if mutating and not self._csrf:
                async with self._auth_lock:
                    if not self._csrf:
                        await self._fetch_csrf()

            headers = kwargs.setdefault("headers", {})
            if self._jwt:
                headers["Authorization"] = f"Bearer {self._jwt}"
            if mutating:
                if self._csrf:
                    headers["X-CSRF-Token"] = self._csrf
                headers.update(self._origin_headers())
            response = await self._send(method, url, **kwargs)
            self._harvest_csrf(response)

            if response.status_code == 401:
                # Expired session — log in again on the next pass.
                self._logged_in = False
                self._jwt = None
                continue
            if self._is_csrf_error(response):
                # Stale CSRF state. First try a fresh token (an authenticated
                # GET also refreshes the paired cookie); if that still fails,
                # tear the whole session down and start over.
                self._csrf = None
                if csrf_failures:
                    self._logged_in = False
                    self._client.cookies.clear()
                csrf_failures += 1
                continue
            return response
        return response

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.is_error:
            try:
                body = response.json()
                # Portainer puts a generic line in `message` ("Unable to update
                # stack") and the actual cause in `details`. Showing only the
                # first tells you nothing you could act on.
                message = body.get("message") or ""
                details = body.get("details") or ""
                if details and details != message:
                    message = f"{message}: {details}" if message else details
                message = message or response.text
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

    async def get_container_info(self, endpoint_id: int, container_id: str) -> dict:
        """Inspect a container on the environment's Docker engine. Its
        Config.Image keeps the reference it was created from even after that
        tag has been re-pulled onto a newer image."""
        response = await self._request(
            "GET", f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/json"
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

    async def prune_images(self, endpoint_id: int, all_unused: bool = False) -> dict:
        """Remove unused images.

        Default (dangling only) removes untagged layers — which includes the
        old image a re-pull leaves behind, since the tag moves to the new one.
        That reclaims update leftovers without touching anything nameable.

        all_unused removes every image no container references. Stopping a
        stack in Portainer REMOVES its containers, so a stopped stack's images
        look unused and get deleted — the stack then can't start until they are
        pulled again. Callers must make that consequence explicit.
        """
        response = await self._request(
            "POST",
            f"/api/endpoints/{endpoint_id}/docker/images/prune",
            params={"filters": json.dumps({"dangling": ["false" if all_unused else "true"]})},
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

    async def set_stack_state(self, stack_id: int, endpoint_id: int, running: bool) -> dict:
        """Start or stop every service in a stack."""
        action = "start" if running else "stop"
        response = await self._request(
            "POST",
            f"/api/stacks/{stack_id}/{action}",
            params={"endpointId": endpoint_id},
            timeout=REDEPLOY_TIMEOUT,
        )
        self._check(response)
        try:
            return response.json()
        except ValueError:
            return {}

    async def set_container_state(
        self, endpoint_id: int, container_id: str, running: bool
    ) -> dict:
        """Start or stop one container via Portainer's Docker proxy."""
        action = "start" if running else "stop"
        response = await self._request(
            "POST",
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/{action}",
            timeout=REDEPLOY_TIMEOUT,
        )
        # 304 means it was already in that state — not an error worth surfacing.
        if response.status_code != 304:
            self._check(response)
        return {}

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
        if tags or digests:
            return (tags or digests)[0]
    except Exception:
        pass
    # Neither: the tag AND the digest reference have both been re-pulled onto a
    # newer image, leaving this one anonymous. The container still remembers
    # what it was created from, which is the only name left that means anything.
    try:
        info = await client.get_container_info(endpoint_id, container.get("Id") or "")
        created_from = ((info.get("Config") or {}).get("Image") or "").strip()
        if created_from and not created_from.startswith("sha256:"):
            return created_from
    except Exception:
        pass
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


def cannot_recreate_image(image: str) -> bool:
    """Images whose container carries the connection used to recreate it.

    Portainer proxies every command for an agent environment *through that
    agent*, so recreating the agent stops the container mid-operation and the
    replacement is never created — Portainer can no longer reach that machine
    to finish, or to undo it. Portainer itself fails the same way for its own
    container. Both leave the environment stopped with a new image pulled and
    unused, and neither can be repaired from here.

    Restruo is not included: recreating itself is disruptive but completes,
    because Portainer — not Restruo — performs it.
    """
    name = (image or "").lower()
    return "portainer/portainer" in name or "portainer/agent" in name


def is_self_critical_image(image: str) -> bool:
    """Images this tool must never stop through its own control path.

    Stopping Portainer through Portainer's API is unrecoverable from here —
    it kills the very API needed to start it again. The same is true of an
    agent, which is that API for its environment. Restruo stopping itself is
    recoverable (from Portainer) but still removes the UI mid-click.
    """
    name = (image or "").lower()
    return cannot_recreate_image(image) or "restruo" in name


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
