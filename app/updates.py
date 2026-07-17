"""Update checker: compares local image digests against registry digests.

Only images tracking :latest (or untagged) are checked; pinned tags are
reported as "pinned" and skipped. Runs on a schedule and on demand; results
are cached in memory for the dashboard.
"""

import asyncio
import logging
import time

from .notifiers import Notifier, UpdateEvent
from .portainer import (
    PortainerClient,
    extract_images,
    normalize_container,
    resolve_image_name,
    standalone_containers,
)
from .registry import RegistryClient, parse_image_ref

logger = logging.getLogger("restruo.updates")

STATUS_UPDATE_AVAILABLE = "update-available"
STATUS_UP_TO_DATE = "up-to-date"
STATUS_PINNED = "pinned"
STATUS_UNKNOWN = "unknown"


class UpdateChecker:
    def __init__(
        self,
        get_clients,
        registry: RegistryClient,
        interval_hours: float,
        notifiers: list[Notifier] | None = None,
        floating_tags: tuple[str, ...] | list[str] = ("latest",),
    ):
        # get_clients: callable returning [(instance_id, PortainerClient), ...],
        # so the checker always sees the current set of managed instances.
        self.get_clients = get_clients
        self.floating_tags = set(floating_tags)
        self.registry = registry
        self.interval_hours = interval_hours
        self.notifiers = notifiers or []
        self.checked_at: float | None = None
        self.results: list[dict] = []
        self.checking = False
        self._lock = asyncio.Lock()
        self._notified: set[tuple] = set()
        # Per-run memos (reset by check_all): the same image often appears in
        # many stacks/instances — ask each registry and docker engine once.
        self._remote_tasks: dict = {}
        self._inspect_tasks: dict = {}

    async def _remote_digest(self, ref) -> str:
        key = (ref.registry, ref.repository, ref.tag)
        task = self._remote_tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(self.registry.get_remote_digest(ref))
            self._remote_tasks[key] = task
        return await task

    async def _image_info(self, client: PortainerClient, endpoint_id: int, image: str) -> dict:
        key = (id(client), endpoint_id, image)
        task = self._inspect_tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(client.get_image_info(endpoint_id, image))
            self._inspect_tasks[key] = task
        return await task

    def snapshot(self) -> dict:
        return {
            "checkedAt": self.checked_at,
            "checking": self.checking,
            "instances": self.results,
        }

    @staticmethod
    def _normalize_image(image: str) -> str:
        base = image.partition("@")[0]
        last = base.rsplit("/", 1)[-1]
        return base if ":" in last else f"{base}:latest"

    async def _running_digests(
        self, client: PortainerClient, endpoint_id: int, raw: str, containers: list[dict]
    ) -> set[str] | None:
        """Repo digests of the image the stack's containers are ACTUALLY running.

        The local tag may already point at a newer pull while the container still
        runs the old image — comparing the running image is what tells the truth.
        Returns None when no matching container exists (fall back to the tag).
        """
        wanted = self._normalize_image(raw)
        image_ids = {
            c.get("ImageID")
            for c in containers
            if c.get("ImageID") and self._normalize_image(c.get("Image", "")) == wanted
        }
        if not image_ids:
            return None
        digests: set[str] = set()
        for image_id in image_ids:
            try:
                info = await self._image_info(client, endpoint_id, image_id)
                digests |= {e.rpartition("@")[2] for e in info.get("RepoDigests") or []}
            except Exception:
                pass
        return digests

    async def _check_image(
        self, client: PortainerClient, endpoint_id: int, raw: str, containers: list[dict]
    ) -> dict:
        ref = parse_image_ref(raw)
        if ref is None:
            return {"image": raw, "status": STATUS_UNKNOWN,
                    "detail": "image reference uses variables or is unparseable"}
        if ref.pinned_digest or ref.tag not in self.floating_tags:
            return {"image": raw, "status": STATUS_PINNED}

        try:
            remote_digest = await self._remote_digest(ref)
        except Exception as exc:
            return {"image": raw, "status": STATUS_UNKNOWN, "detail": f"registry: {exc}"}

        local_digests = await self._running_digests(client, endpoint_id, raw, containers)
        if local_digests is None:
            # No matching container found — fall back to what the tag points at.
            try:
                info = await self._image_info(client, endpoint_id, raw)
                local_digests = {
                    entry.rpartition("@")[2] for entry in info.get("RepoDigests") or []
                }
            except Exception as exc:
                return {"image": raw, "status": STATUS_UNKNOWN, "detail": f"local image: {exc}"}

        if not local_digests:
            # Locally built image with no repo digest — nothing to compare.
            return {"image": raw, "status": STATUS_UNKNOWN, "detail": "no local repo digest"}

        if remote_digest in local_digests:
            return {"image": raw, "status": STATUS_UP_TO_DATE}
        # Show both digests so a stuck badge is diagnosable from the tooltip.
        local_short = ", ".join(sorted(d.removeprefix("sha256:")[:12] for d in local_digests))
        return {
            "image": raw,
            "status": STATUS_UPDATE_AVAILABLE,
            "detail": f"running {local_short} · registry {remote_digest.removeprefix('sha256:')[:12]}",
        }

    @staticmethod
    def _stack_containers(stack: dict, containers: list[dict]) -> list[dict]:
        name = stack.get("Name", "")
        out = []
        for c in containers:
            labels = c.get("Labels") or {}
            if labels.get("com.docker.compose.project") == name or \
                    labels.get("com.docker.stack.namespace") == name:
                out.append(c)
        return out

    async def _check_instance(self, iid: int, client: PortainerClient) -> dict:
        result = {
            "instance": {"id": iid, "name": client.instance.name},
            "stacks": [],
            "containers": [],
            "error": None,
        }
        try:
            stacks = await client.list_stacks()
        except Exception as exc:
            result["error"] = str(exc)
            return result

        container_tasks: dict[int, asyncio.Task] = {}

        def containers_for(endpoint_id: int) -> asyncio.Task:
            if endpoint_id not in container_tasks:
                async def fetch() -> list[dict]:
                    try:
                        return await client.list_containers(endpoint_id)
                    except Exception:
                        return []
                container_tasks[endpoint_id] = asyncio.ensure_future(fetch())
            return container_tasks[endpoint_id]

        # Be gentle with each Portainer: bounded concurrency per instance.
        semaphore = asyncio.Semaphore(6)

        async def check_image_bounded(endpoint_id: int, raw: str, containers: list[dict]) -> dict:
            async with semaphore:
                return await self._check_image(client, endpoint_id, raw, containers)

        async def check_stack(stack: dict) -> dict:
            stack_containers = self._stack_containers(
                stack, await containers_for(stack["EndpointId"])
            )
            try:
                images = extract_images(await client.get_stack_file(stack["Id"]))
            except Exception:
                images = []
            checked = list(await asyncio.gather(
                *(check_image_bounded(stack["EndpointId"], raw, stack_containers)
                  for raw in images)
            ))
            return {
                "id": stack["Id"],
                "name": stack.get("Name", ""),
                "images": checked,
                "updatesAvailable": sum(
                    1 for c in checked if c["status"] == STATUS_UPDATE_AVAILABLE
                ),
            }

        result["stacks"] = list(await asyncio.gather(*(check_stack(s) for s in stacks)))

        # Containers that live outside any Portainer stack.
        stack_names = {s.get("Name") for s in stacks}
        try:
            endpoint_ids = [e["Id"] for e in await client.list_endpoints()]
        except Exception:
            endpoint_ids = list(container_tasks)

        async def check_standalone(endpoint_id: int, raw_container: dict) -> dict:
            normalized = normalize_container(raw_container, endpoint_id)
            normalized["image"] = await resolve_image_name(
                client, endpoint_id, raw_container
            )
            checked = await check_image_bounded(
                endpoint_id, normalized["image"], [raw_container]
            )
            return {**normalized, **checked}

        standalone_jobs = []
        for endpoint_id in endpoint_ids:
            for raw_container in standalone_containers(
                await containers_for(endpoint_id), stack_names
            ):
                standalone_jobs.append(check_standalone(endpoint_id, raw_container))
        result["containers"] = list(await asyncio.gather(*standalone_jobs))
        return result

    def mark_updated(
        self, iid: int, stack_id: int | None = None, container_id: str | None = None
    ) -> None:
        """Reflect a successful repull+redeploy in the cached results so badges
        clear immediately instead of waiting for the next registry check."""
        for instance_result in self.results:
            if instance_result["instance"]["id"] != iid:
                continue
            if stack_id is not None:
                for stack in instance_result["stacks"]:
                    if stack["id"] == stack_id:
                        for image in stack["images"]:
                            if image["status"] == STATUS_UPDATE_AVAILABLE:
                                image["status"] = STATUS_UP_TO_DATE
                        stack["updatesAvailable"] = 0
            if container_id is not None:
                for container in instance_result.get("containers", []):
                    if container["id"] == container_id and \
                            container["status"] == STATUS_UPDATE_AVAILABLE:
                        container["status"] = STATUS_UP_TO_DATE

    async def check_all(self) -> dict:
        async with self._lock:
            self.checking = True
            self._remote_tasks = {}
            self._inspect_tasks = {}
            try:
                self.results = list(
                    await asyncio.gather(
                        *(
                            self._check_instance(iid, client)
                            for iid, client in self.get_clients()
                        )
                    )
                )
                self.checked_at = time.time()
                await self._notify_new()
            finally:
                self.checking = False
        return self.snapshot()

    async def _notify_new(self) -> None:
        current: set[tuple] = set()
        events: list[UpdateEvent] = []
        for instance_result in self.results:
            iid = instance_result["instance"]["id"]
            for stack in instance_result["stacks"]:
                for image in stack["images"]:
                    if image["status"] != STATUS_UPDATE_AVAILABLE:
                        continue
                    key = (iid, stack["id"], image["image"])
                    current.add(key)
                    if key not in self._notified:
                        events.append(UpdateEvent(
                            instance_name=instance_result["instance"]["name"],
                            stack_name=stack["name"],
                            image=image["image"],
                        ))
            for container in instance_result.get("containers", []):
                if container["status"] != STATUS_UPDATE_AVAILABLE:
                    continue
                key = (iid, "container", container["id"], container["image"])
                current.add(key)
                if key not in self._notified:
                    events.append(UpdateEvent(
                        instance_name=instance_result["instance"]["name"],
                        stack_name=container["name"],
                        image=container["image"],
                    ))
        # Forget resolved updates so they re-notify if they reappear later.
        self._notified = current
        if not events:
            return
        for notifier in self.notifiers:
            try:
                await notifier.send(events)
            except Exception:
                logger.exception("Notifier %s failed", type(notifier).__name__)

    async def run_periodic(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:
                logger.exception("Scheduled update check failed")
            await asyncio.sleep(self.interval_hours * 3600)
