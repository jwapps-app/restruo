"""Async client for a single Portainer instance.

A redeploy branches on how the stack was created: a git stack is told to
pull and redeploy, a compose stack is re-sent its file with PullImage set.
Field names in request bodies are PascalCase and case-sensitive per
Portainer's API.

Two auth modes: an API token sent as X-API-Key, or username/password exchanged
at /api/auth for a session JWT that is refreshed automatically on expiry.
Secrets are never logged.
"""

import asyncio
import base64
import json
import logging
import re

import httpx
import yaml

from . import timing
from .errors import PortainerError, describe
from .health import container_is_down

logger = logging.getLogger("restruo.portainer")

STACK_TYPE_NAMES = {1: "swarm", 2: "compose", 3: "kubernetes"}
STACK_INACTIVE = 2          # Portainer's Status for a stack that has been stopped
STACK_STATUS_NAMES = {1: "active", STACK_INACTIVE: "inactive"}

_IMAGE_RE = re.compile(r"^\s*image\s*:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


def extract_images(stack_file_content: str) -> list[str]:
    """Every service's image, in file order, each once.

    Parsed as YAML — compose files are YAML, and a line-based regex misses
    flow syntax (`services: {web: {image: nginx}}`) and matches `image:` in
    places that are not services. The regex remains as a fallback for a file
    that does not parse, so a broken file still yields what can be read.
    """
    seen: dict[str, None] = {}
    try:
        doc = yaml.safe_load(stack_file_content or "")
    except yaml.YAMLError:
        doc = None
    services = doc.get("services") if isinstance(doc, dict) else None
    if isinstance(services, dict):
        for service in services.values():
            image = service.get("image") if isinstance(service, dict) else None
            if isinstance(image, str) and image.strip():
                seen.setdefault(image.strip())
        return list(seen)
    for match in _IMAGE_RE.findall(stack_file_content or ""):
        seen.setdefault(match)
    return list(seen)


# ${VAR}, ${VAR:-default}, ${VAR-default}, ${VAR:?msg}, ${VAR?msg}, $VAR
_INTERPOLATE_RE = re.compile(
    r"\$\{([A-Za-z_]\w*)(?:(:-|-|:\?|\?)([^}]*))?\}|\$([A-Za-z_]\w*)"
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
    the raw file alone says nothing about what is actually running. Follows
    compose: `:-` falls back when unset or empty, `-` only when unset, and
    `:?` / `?` are errors when unset (or empty) — not defaults. Returns None
    when a variable cannot be resolved, because substituting empty text
    would silently produce a nonsense reference like ":latest".
    """
    unresolved = False

    def replace(match: re.Match) -> str:
        nonlocal unresolved
        name = match.group(1) or match.group(4)
        op, arg = match.group(2), match.group(3)
        present = name in env
        current = env.get(name, "")
        if op == ":-":
            return current if current else arg
        if op == "-":
            return current if present else arg
        if op in (":?", "?"):
            ok = bool(current) if op == ":?" else present
            if ok:
                return current
            unresolved = True
            return ""
        if current:
            return current
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
    if images and all(image is not None for image in images):
        return images
    # Nothing declared (no file, or a file that could not be read) or
    # something unresolved: what the containers run is the ground truth.

    resolved = [image for image in images if image is not None]
    for container in containers:
        image = container.get("Image") or ""
        if image and image not in resolved:
            resolved.append(image)
    # Nothing to fall back on (a stopped stack, say) — keep the raw references
    # so the row still shows something recognisable.
    return resolved or declared


PREDEFINED_NETWORKS = ("bridge", "host", "none")


def networks_safe_to_remove(networks: list[dict], containers: list[dict]) -> list[dict]:
    """Networks that no container — running or stopped — refers to.

    Docker's built-in networks are never candidates, and neither is anything
    that is not a plain local network: swarm and ingress networks belong to
    the cluster, not to the containers on this one host.
    """
    used_ids: set[str] = set()
    used_names: set[str] = set()
    for container in containers:
        mode = (container.get("HostConfig") or {}).get("NetworkMode")
        if mode:
            used_names.add(mode)
        attached = (container.get("NetworkSettings") or {}).get("Networks") or {}
        for name, settings in attached.items():
            used_names.add(name)
            if isinstance(settings, dict) and settings.get("NetworkID"):
                used_ids.add(settings["NetworkID"])
    out = []
    for network in networks:
        name, network_id = network.get("Name"), network.get("Id")
        if not isinstance(network_id, str) or not network_id:
            continue
        if name in PREDEFINED_NETWORKS or network.get("Ingress"):
            continue
        if (network.get("Scope") or "local") != "local":
            continue
        if network_id in used_ids or name in used_names:
            continue
        out.append(network)
    return out


def registry_id_for(image: str, registries: list[dict]) -> int | None:
    """The Portainer registry whose login applies to this image, if any."""
    # Same rule the registry client uses: the first path segment is a host
    # only when it looks like one; otherwise the image lives on Docker Hub.
    first, _, rest = (image or "").partition("/")
    host = first.lower() if rest and ("." in first or ":" in first or first == "localhost") \
        else "docker.io"
    for registry in registries:
        if not registry.get("Authentication") or not _is_int(registry.get("Id")):
            continue
        url = str(registry.get("URL") or "").lower()
        url = url.split("://", 1)[-1].split("/", 1)[0]
        if url in ("registry-1.docker.io", "index.docker.io", "registry.hub.docker.com"):
            url = "docker.io"
        if url and url == host:
            return registry["Id"]
    return None


def profiled_images(file_content: str, env: dict[str, str] | None = None) -> set[str]:
    """Images of services that compose will not start.

    A service with a `profiles:` list only runs when one of those profiles is
    activated, and Portainer never activates any — so such a service has never
    been deployed and its image has never been pulled. Asking the host about
    it gets a 404, which is not a failed check but a service that is not
    running, and must be reported as such.
    """
    try:
        doc = yaml.safe_load(file_content or "")
    except yaml.YAMLError:
        return set()
    services = (doc or {}).get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return set()
    out: set[str] = set()
    for service in services.values():
        if not isinstance(service, dict) or not service.get("profiles"):
            continue
        image = service.get("image")
        if isinstance(image, str) and image.strip():
            image = image.strip()
            out.add(image)
            resolved = interpolate(image, env or {})
            if resolved:
                out.add(resolved)
    return out


_CONTAINER_ID_RE = re.compile(r"[0-9a-fA-F]{12,64}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def well_formed(items, kind: str, check) -> list[dict]:
    """Keep only entries whose identifiers have the shape the rest of the code
    assumes. Ids end up in URLs and in the page; one that is not a number or
    a hex string is not something to act on, whatever sent it."""
    kept = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and check(item):
            kept.append(item)
        else:
            logger.warning("Ignoring a %s with a malformed id: %r", kind, str(item)[:80])
    return kept


def stack_is_well_formed(stack: dict) -> bool:
    return _is_int(stack.get("Id")) and _is_int(stack.get("EndpointId"))


def endpoint_is_well_formed(endpoint: dict) -> bool:
    return _is_int(endpoint.get("Id"))


def container_is_well_formed(container: dict) -> bool:
    cid = container.get("Id")
    return isinstance(cid, str) and bool(_CONTAINER_ID_RE.fullmatch(cid))


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
        self._retiring: set[asyncio.Future] = set()

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
            timeout=timing.READ_TIMEOUT,
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
        # Requests already in flight on the old pool — a deploy that has
        # reached Portainer, say — finish on it. It is closed once they can
        # all be over, not the moment a probe elsewhere failed.
        self._retiring.add(asyncio.ensure_future(self._close_later(old)))

    async def _close_later(self, client: httpx.AsyncClient) -> None:
        try:
            await asyncio.sleep(timing.POOL_RETIRE_SECONDS)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # The request never reached Portainer, so sending it again is safe
            # for any method: rebuild the pool and give it one more go, so a
            # machine that just came back recovers on this poll rather than
            # needing the instance re-saved. A read timeout or a dropped
            # connection is a different case — the server may already have
            # acted on the first attempt, and a PUT or POST replayed then is a
            # second deploy or a second prune. Those are left to fail.
            await self.reconnect()
            return await self._client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        for task in list(self._retiring):
            task.cancel()
        self._retiring.clear()
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
        return well_formed(response.json(), "environment", endpoint_is_well_formed)

    async def list_stacks(self) -> list[dict]:
        response = await self._request("GET", "/api/stacks")
        self._check(response)
        return well_formed(response.json(), "stack", stack_is_well_formed)

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
        return well_formed(response.json(), "container", container_is_well_formed)

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
            timeout=timing.REDEPLOY_TIMEOUT,
        )
        self._check(response)
        return response.json()

    async def list_networks(self, endpoint_id: int) -> list[dict]:
        response = await self._request(
            "GET", f"/api/endpoints/{endpoint_id}/docker/networks"
        )
        self._check(response)
        body = response.json()
        return body if isinstance(body, list) else []

    async def remove_network(self, endpoint_id: int, network_id: str) -> None:
        response = await self._request(
            "DELETE", f"/api/endpoints/{endpoint_id}/docker/networks/{network_id}"
        )
        self._check(response)

    async def remove_unused_networks(self, endpoint_id: int) -> tuple[int, list[str]]:
        """Remove networks no container refers to. Returns (removed, errors).

        Not Docker's own prune: that counts a network as unused when nothing
        *running* is attached, so it deletes the network of a container that
        is merely stopped — or that crashed a moment ago — and that container
        can then never start again ("network … not found"). Here a network is
        kept if any container, running or not, still points at it. If the
        containers cannot be listed, nothing is removed.
        """
        containers = await self.list_containers(endpoint_id)
        networks = await self.list_networks(endpoint_id)
        removed, errors = 0, []
        for network in networks_safe_to_remove(networks, containers):
            try:
                await self.remove_network(endpoint_id, network["Id"])
                removed += 1
            except Exception as exc:
                errors.append(f"{network.get('Name')}: {describe(exc)}")
        return removed, errors

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
                timeout=timing.REDEPLOY_TIMEOUT,
            )
            self._check(response)
        except PortainerError as exc:
            if exc.status_code != 400:
                raise
            response = await self._request(
                "POST",
                f"/api/endpoints/{endpoint_id}/docker/volumes/prune",
                timeout=timing.REDEPLOY_TIMEOUT,
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
            timeout=timing.REDEPLOY_TIMEOUT,
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
            # An explicit empty object, as Portainer's own UI sends. Relayed
            # through an agent, a POST with no body reaches the Engine with an
            # unknown length, which it rejects on start as a "non-empty request
            # body" — so starting a container on an agent environment failed.
            json={},
            timeout=timing.REDEPLOY_TIMEOUT,
        )
        # 304 means it was already in that state — not an error worth surfacing.
        if response.status_code != 304:
            self._check(response)
        return {}

    # --- plain Docker calls through Portainer's proxy, used to run the helper
    # that replaces containers Portainer cannot recreate itself ---------------

    async def list_registries(self) -> list[dict]:
        """Registries configured in Portainer itself (Registries in its menu)."""
        response = await self._request("GET", "/api/registries")
        self._check(response)
        body = response.json()
        return body if isinstance(body, list) else []

    async def pull_image(
        self, endpoint_id: int, image: str, registry_id: int | None = None
    ) -> None:
        """Pull an image on the environment. `registry_id` names a registry
        configured in Portainer: its proxy swaps that reference for the stored
        login, which is how Portainer's own UI pulls a private image."""
        headers = {}
        if registry_id is not None:
            headers["X-Registry-Auth"] = base64.b64encode(
                json.dumps({"registryId": registry_id}).encode()
            ).decode()
        response = await self._request(
            "POST",
            f"/api/endpoints/{endpoint_id}/docker/images/create",
            params={"fromImage": image},
            headers=headers,
            timeout=timing.REDEPLOY_TIMEOUT,
        )
        self._check(response)
        # The Engine answers 200 and reports a failed pull inside the stream.
        for line in response.text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("error"):
                raise PortainerError(502, f"pull {image}: {event['error']}")

    async def create_container(self, endpoint_id: int, name: str, body: dict) -> str:
        response = await self._request(
            "POST",
            f"/api/endpoints/{endpoint_id}/docker/containers/create",
            params={"name": name},
            json=body,
            timeout=timing.REDEPLOY_TIMEOUT,
        )
        self._check(response)
        return response.json()["Id"]

    async def remove_container(
        self, endpoint_id: int, container_id: str, force: bool = False
    ) -> None:
        response = await self._request(
            "DELETE",
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}",
            params={"force": "1" if force else "0"},
        )
        if response.status_code != 404:
            self._check(response)

    async def container_logs(self, endpoint_id: int, container_id: str) -> str:
        response = await self._request(
            "GET",
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/logs",
            params={"stdout": "1", "stderr": "1", "tail": "50"},
        )
        self._check(response)
        return demux_docker_logs(response.content)

    async def recreate_container(self, endpoint_id: int, container_id: int | str) -> dict:
        """Portainer's own recreate action: pulls the image fresh and recreates
        the container with its existing configuration (same as the UI's
        Recreate button with 're-pull image' enabled)."""
        response = await self._request(
            "POST",
            f"/api/docker/{endpoint_id}/containers/{container_id}/recreate",
            json={"PullImage": True},
            timeout=timing.REDEPLOY_TIMEOUT,
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
            timeout=timing.REDEPLOY_TIMEOUT,
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
            timeout=timing.REDEPLOY_TIMEOUT,
        )
        self._check(response)
        return response.json()

    async def update_stack(self, stack: dict) -> dict:
        """Repull + redeploy one stack, branching on GitConfig.

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


def demux_docker_logs(raw: bytes) -> str:
    """Logs of a container without a TTY arrive as frames: one byte for the
    stream, three of padding, a four-byte big-endian length, then the text."""
    out, pos = [], 0
    while pos + 8 <= len(raw) and raw[pos] in (0, 1, 2) and raw[pos + 1:pos + 4] == b"\0\0\0":
        size = int.from_bytes(raw[pos + 4:pos + 8], "big")
        out.append(raw[pos + 8:pos + 8 + size])
        pos += 8 + size
    if pos == 0:
        return raw.decode(errors="replace")  # a TTY stream: plain text already
    return b"".join(out).decode(errors="replace")


async def resolve_image_name(
    client: PortainerClient, endpoint_id: int, container: dict
) -> str:
    """Containers whose image tag was re-pulled elsewhere report a bare sha256
    digest as their image. Resolve it back to the reference the container was
    created from, so display, update checks, and guards keep working.

    The container's Config.Image is asked first: it is the operator's own
    choice of tag, and the one thing that decides whether the image floats.
    The old image's RepoTags/RepoDigests are a fallback — a digest reference
    from there would read as pinned when the operator chose `latest`.
    """
    image = container.get("Image") or ""
    if not image.startswith("sha256:"):
        return image
    try:
        info = await client.get_container_info(endpoint_id, container.get("Id") or "")
        created_from = ((info.get("Config") or {}).get("Image") or "").strip()
        if created_from and not created_from.startswith("sha256:"):
            return created_from
    except Exception:
        pass
    try:
        info = await client.get_image_info(endpoint_id, container.get("ImageID") or image)
        tags = info.get("RepoTags") or []
        digests = info.get("RepoDigests") or []
        if tags or digests:
            return (tags or digests)[0]
    except Exception:
        pass
    return image


def stack_names_by_endpoint(stacks: list[dict]) -> dict[int, set[str]]:
    """Which Portainer stack names live on which environment. A compose
    project named like a stack on *another* environment is not that stack."""
    out: dict[int, set[str]] = {}
    for stack in stacks:
        out.setdefault(stack.get("EndpointId"), set()).add(stack.get("Name"))
    return out


def standalone_containers(containers: list[dict], stack_names: set[str]) -> list[dict]:
    """Containers that don't belong to any Portainer stack on this environment."""
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


def helper_kind(image: str) -> str:
    """"portainer", "agent", or "" — which of the two containers that cannot
    survive their own recreate this image is, if either. They are told apart
    because replacing Portainer migrates its database and an agent has none."""
    name = (image or "").lower()
    if "portainer/portainer" in name:
        return "portainer"
    if "portainer/agent" in name:
        return "agent"
    return ""


def is_restruo_image(image: str) -> bool:
    return "restruo" in (image or "").lower()


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
    return bool(helper_kind(image))


def is_self_critical_image(image: str) -> bool:
    """Images this tool must never stop through its own control path.

    Stopping Portainer through Portainer's API is unrecoverable from here —
    it kills the very API needed to start it again. The same is true of an
    agent, which is that API for its environment. Restruo stopping itself is
    recoverable (from Portainer) but still removes the UI mid-click.
    """
    return cannot_recreate_image(image) or is_restruo_image(image)


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
