"""Update checker: compares local image digests against registry digests.

Only images tracking :latest (or untagged) are checked; pinned tags are
reported as "pinned" and skipped. Runs on a schedule and on demand; results
are cached in memory for the dashboard.
"""

import asyncio
import logging
import time

from .notifiers import Notifier, UpdateEvent
from .portainer import PortainerClient, extract_images
from .registry import RegistryClient, parse_image_ref

logger = logging.getLogger("restack.updates")

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
    ):
        # get_clients: callable returning [(instance_id, PortainerClient), ...],
        # so the checker always sees the current set of managed instances.
        self.get_clients = get_clients
        self.registry = registry
        self.interval_hours = interval_hours
        self.notifiers = notifiers or []
        self.checked_at: float | None = None
        self.results: list[dict] = []
        self.checking = False
        self._lock = asyncio.Lock()
        self._notified: set[tuple[int, int, str]] = set()

    def snapshot(self) -> dict:
        return {
            "checkedAt": self.checked_at,
            "checking": self.checking,
            "instances": self.results,
        }

    async def _check_image(self, client: PortainerClient, endpoint_id: int, raw: str) -> dict:
        ref = parse_image_ref(raw)
        if ref is None:
            return {"image": raw, "status": STATUS_UNKNOWN,
                    "detail": "image reference uses variables or is unparseable"}
        if not ref.tracks_latest:
            return {"image": raw, "status": STATUS_PINNED}

        try:
            remote_digest = await self.registry.get_remote_digest(ref)
        except Exception as exc:
            return {"image": raw, "status": STATUS_UNKNOWN, "detail": f"registry: {exc}"}

        try:
            info = await client.get_image_info(endpoint_id, raw)
            local_digests = {
                entry.rpartition("@")[2] for entry in info.get("RepoDigests") or []
            }
        except Exception as exc:
            return {"image": raw, "status": STATUS_UNKNOWN, "detail": f"local image: {exc}"}

        if not local_digests:
            # Locally built image with no repo digest — nothing to compare.
            return {"image": raw, "status": STATUS_UNKNOWN, "detail": "no local repo digest"}

        status = STATUS_UP_TO_DATE if remote_digest in local_digests else STATUS_UPDATE_AVAILABLE
        return {"image": raw, "status": status}

    async def _check_instance(self, iid: int, client: PortainerClient) -> dict:
        result = {
            "instance": {"id": iid, "name": client.instance.name},
            "stacks": [],
            "error": None,
        }
        try:
            stacks = await client.list_stacks()
        except Exception as exc:
            result["error"] = str(exc)
            return result

        for stack in stacks:
            try:
                images = extract_images(await client.get_stack_file(stack["Id"]))
            except Exception:
                images = []
            checked = [
                await self._check_image(client, stack["EndpointId"], raw) for raw in images
            ]
            result["stacks"].append({
                "id": stack["Id"],
                "name": stack.get("Name", ""),
                "images": checked,
                "updatesAvailable": sum(
                    1 for c in checked if c["status"] == STATUS_UPDATE_AVAILABLE
                ),
            })
        return result

    async def check_all(self) -> dict:
        async with self._lock:
            self.checking = True
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
        current: set[tuple[int, int, str]] = set()
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
