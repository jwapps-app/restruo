"""Update checker: compares local image digests against registry digests.

Images on a channel tag (latest, lts, stable, … — see config.MOVING_TAGS, or
RESTRUO_FLOATING_TAGS) are checked; a tag naming a version is reported as
"pinned" and left alone. Runs on a schedule and on demand; results are
cached in memory for the dashboard.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from .config import UpdatesConfig
from .errors import RegistryError, describe
from .inventory import StackEntry, read_inventory
from .notifiers import Notifier, UpdateEvent
from .portainer import PortainerClient, normalize_container, profiled_images, stack_env
from .registry import RegistryClient, parse_image_ref

logger = logging.getLogger("restruo.updates")

STATUS_UPDATE_AVAILABLE = "update-available"
STATUS_UP_TO_DATE = "up-to-date"
STATUS_PINNED = "pinned"
STATUS_UNKNOWN = "unknown"
STATUS_LOCAL = "local"
STATUS_PRIVATE = "private"
STATUS_NOT_DEPLOYED = "not-deployed"


class UpdateChecker:
    def __init__(
        self,
        get_clients,
        registry: RegistryClient,
        settings: UpdatesConfig,
        notifiers: list[Notifier] | None = None,
        state_path: Path | None = None,
    ):
        # get_clients: callable returning [(instance_id, PortainerClient), ...],
        # so the checker always sees the current set of managed instances.
        self.get_clients = get_clients
        # settings is config.updates: which tags float, whether version tags
        # are checked too, and how often a scheduled check runs.
        self.floating_tags = set(settings.floating_tags)
        self.check_version_tags = settings.check_version_tags
        self.registry = registry
        self.interval_hours = settings.interval_hours
        self.notifiers = notifiers or []
        self.checked_at: float | None = None
        self.results: list[dict] = []
        self.checking = False
        self._lock = asyncio.Lock()
        # What has already been announced. Persisted, so a restart does not
        # re-send a mail for every update that was already known.
        self._state_path = state_path
        self._notified: set[tuple] = self._load_notified()
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

    def _load_notified(self) -> set[tuple]:
        if self._state_path is None or not self._state_path.is_file():
            return set()
        try:
            return {tuple(item) for item in json.loads(self._state_path.read_text())}
        except (ValueError, TypeError):
            return set()

    def _save_notified(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            entries = sorted((list(k) for k in self._notified), key=json.dumps)
            tmp.write_text(json.dumps(entries))
            tmp.replace(self._state_path)
        except OSError:
            logger.exception("Could not persist notification state")

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

    def _running_image_ids(self, raw: str, containers: list[dict], ref_of) -> set[str] | None:
        """Image ids of the containers running this reference, or None if none
        of them do. `ref_of(container)` is the reference a container was
        created from — its Image field is a bare id once its tag has moved,
        which matches no image name, and that container is the one that most
        needs to be in this comparison."""
        wanted = self._normalize_image(raw)
        ids = {
            container["ImageID"] for container in containers
            if container.get("ImageID") and self._normalize_image(ref_of(container)) == wanted
        }
        return ids or None

    async def _running_digests(
        self, client: PortainerClient, endpoint_id: int, image_ids: set[str] | None
    ) -> dict[str, set[str]] | None:
        """Repo digests of each image the containers are ACTUALLY running,
        keyed by image id — every replica judged on its own, so one current
        replica cannot vouch for an old one beside it.

        The local tag may already point at a newer pull while the container
        still runs the old image — comparing the running image is what tells
        the truth. Returns None when no matching container exists (fall back
        to the tag). Raises if an image cannot be inspected: an unreadable
        image is not a locally built one.
        """
        if not image_ids:
            return None
        out: dict[str, set[str]] = {}
        for image_id in image_ids:
            info = await self._image_info(client, endpoint_id, image_id)
            out[image_id] = {e.rpartition("@")[2] for e in info.get("RepoDigests") or []}
        return out

    async def _check_image(
        self, client: PortainerClient, endpoint_id: int, raw: str, containers: list[dict],
        ref_of,
    ) -> dict:
        ref = parse_image_ref(raw)
        if ref is None:
            return {"image": raw, "status": STATUS_UNKNOWN,
                    "detail": "image reference uses variables or is unparseable"}
        if ref.pinned_digest:
            return {"image": raw, "status": STATUS_PINNED,
                    "detail": "Pinned to a digest — it names one exact image and cannot change"}
        # Three kinds of tag. A channel tag (latest, stable…) moves by design
        # and is always checked. A version tag (10.11, 16-alpine) looks fixed
        # but is republished under the same name — patch releases, base-image
        # fixes — so it is checked when asked. Either way the test is the
        # same: does the registry's digest for this tag match what is running.
        version_tag = ref.tag not in self.floating_tags
        if version_tag and not self.check_version_tags:
            return {"image": raw, "status": STATUS_PINNED,
                    "detail": "Version tag — not checked. It can still be republished under "
                              "the same name; set RESTRUO_CHECK_VERSION_TAGS=true to watch for that"}

        # Local first: an image with no repo digest never came from a registry
        # (built on the box, or created by the NAS itself), so there is nothing
        # to compare against and no point asking a registry about it.
        running_ids = self._running_image_ids(raw, containers, ref_of)
        try:
            per_image = await self._running_digests(client, endpoint_id, running_ids)
        except Exception as exc:
            return {"image": raw, "status": STATUS_UNKNOWN,
                    "detail": f"running image: {describe(exc)}"}
        local_digests = set().union(*per_image.values()) if per_image else None
        if local_digests is None:
            # No matching container found — fall back to what the tag points at.
            try:
                info = await self._image_info(client, endpoint_id, raw)
                local_digests = {
                    entry.rpartition("@")[2] for entry in info.get("RepoDigests") or []
                }
            except Exception as exc:
                return {"image": raw, "status": STATUS_UNKNOWN,
                        "detail": f"local image: {describe(exc)}"}

        if not local_digests:
            # No repo digest at all. Either the image was built on the box, or
            # its tag has since been re-pulled onto a newer image — which strips
            # the old one of both its tag and its digest. In that case the new
            # image is already on the host and only the container is behind, so
            # this is an update waiting to be applied, not a local build.
            running_ids = running_ids or set()
            tag_id = None
            try:
                tag_id = (await self._image_info(client, endpoint_id, raw)).get("Id")
            except Exception:
                pass
            if tag_id and running_ids and tag_id not in running_ids:
                running_short = ", ".join(
                    sorted(i.removeprefix("sha256:")[:12] for i in running_ids)
                )
                return {
                    "image": raw,
                    "status": STATUS_UPDATE_AVAILABLE,
                    "detail": f"running {running_short} · {tag_id.removeprefix('sha256:')[:12]} "
                              "already pulled — recreate to apply",
                    **({"rebuild": True} if version_tag else {}),
                }
            return {"image": raw, "status": STATUS_LOCAL,
                    "detail": "built or loaded locally — not published to a registry"}

        try:
            remote_digest = await self._remote_digest(ref)
        except RegistryError as exc:
            if exc.status_code in (401, 403):
                return {"image": raw, "status": STATUS_PRIVATE,
                        "detail": f"{ref.registry} needs credentials for this image — "
                                  "set RESTRUO_REGISTRY_AUTH"}
            if exc.status_code == 429:
                return {"image": raw, "status": STATUS_UNKNOWN,
                        "detail": f"{ref.registry} is rate-limiting this address — try again later"}
            return {"image": raw, "status": STATUS_UNKNOWN,
                    "detail": f"{ref.registry}: {describe(exc)}"}
        except Exception as exc:
            return {"image": raw, "status": STATUS_UNKNOWN,
                    "detail": f"{ref.registry}: {describe(exc)}"}

        behind = ({i for i, d in (per_image or {}).items() if remote_digest not in d}
                  if per_image else ({"tag"} if remote_digest not in local_digests else set()))
        if not behind:
            return {"image": raw, "status": STATUS_UP_TO_DATE}
        # Show both digests so a stuck badge is diagnosable from the tooltip.
        shown = ({d for i in behind for d in per_image[i]} if per_image else local_digests)
        local_short = ", ".join(sorted(d.removeprefix("sha256:")[:12] for d in shown)) or "no digest"
        if version_tag:
            # Same tag, different image: the name did not move, its contents did.
            return {
                "image": raw,
                "status": STATUS_UPDATE_AVAILABLE,
                "rebuild": True,
                "detail": f"a newer build was published under the same tag · running "
                          f"{local_short} · registry {remote_digest.removeprefix('sha256:')[:12]}",
            }
        return {
            "image": raw,
            "status": STATUS_UPDATE_AVAILABLE,
            "detail": f"running {local_short} · registry {remote_digest.removeprefix('sha256:')[:12]}",
        }

    async def _check_instance(self, iid: int, client: PortainerClient) -> dict:
        result = {
            "instance": {"id": iid, "name": client.instance.name},
            "stacks": [],
            "containers": [],
            "error": None,
        }
        try:
            inventory = await read_inventory(client)
        except Exception as exc:
            result["error"] = describe(exc)
            return result

        # Be gentle with each Portainer: bounded concurrency per instance.
        semaphore = asyncio.Semaphore(6)

        async def check_image_bounded(endpoint_id: int, raw: str, containers: list[dict]) -> dict:
            def ref_of(container: dict) -> str:
                return inventory.ref(endpoint_id, container)

            async with semaphore:
                return await self._check_image(client, endpoint_id, raw, containers, ref_of)

        async def check_stack(entry: StackEntry) -> dict:
            stack, endpoint_id = entry.stack, entry.stack["EndpointId"]
            if endpoint_id in inventory.container_errors:
                # Not "no containers": "could not look". Everything on this
                # environment reads as unchecked, not as fine.
                reason = inventory.container_errors[endpoint_id]
                return {
                    "id": stack["Id"], "name": stack.get("Name", ""),
                    "images": [{"image": raw, "status": STATUS_UNKNOWN,
                                "detail": f"could not list this environment's containers: {reason}"}
                               for raw in entry.images],
                    "updatesAvailable": 0,
                }
            # A service behind a compose profile is never started by Portainer:
            # its image is declared but not deployed, and there is nothing on
            # the host to compare. Say so instead of reporting a failed check.
            inactive = profiled_images(entry.file, stack_env(stack))
            running = {
                self._normalize_image(inventory.ref(endpoint_id, c)) for c in entry.containers
            }

            async def check_or_skip(raw: str) -> dict:
                # Behind a profile — unless some active service runs the same
                # image, in which case there is a container to check.
                if raw in inactive and self._normalize_image(raw) not in running:
                    return {"image": raw, "status": STATUS_NOT_DEPLOYED,
                            "detail": "service is behind a compose profile, which "
                                      "Portainer never activates — not running"}
                return await check_image_bounded(endpoint_id, raw, entry.containers)

            checked = list(await asyncio.gather(*(check_or_skip(raw) for raw in entry.images)))
            return {
                "id": stack["Id"],
                "name": stack.get("Name", ""),
                "images": checked,
                "updatesAvailable": sum(
                    1 for c in checked if c["status"] == STATUS_UPDATE_AVAILABLE
                ),
            }

        async def check_standalone(endpoint_id: int, container: dict) -> dict:
            normalized = normalize_container(container, endpoint_id)
            normalized["image"] = inventory.ref(endpoint_id, container)
            checked = await check_image_bounded(endpoint_id, normalized["image"], [container])
            environment = inventory.environments.get(endpoint_id)
            return {**normalized, **checked,
                    **({"environment": environment} if environment else {})}

        result["stacks"], result["containers"] = await asyncio.gather(
            asyncio.gather(*(check_stack(entry) for entry in inventory.stacks)),
            asyncio.gather(*(check_standalone(e, c) for e, c in inventory.standalone)),
        )
        if inventory.container_errors:
            result["environmentErrors"] = {
                inventory.environment_name(endpoint_id): reason
                for endpoint_id, reason in inventory.container_errors.items()
            }
        return result

    def mark_updated(
        self, iid: int, stack_id: int | None = None, container_id: str | None = None,
        endpoint_id: int | None = None,
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
                    # Cloned hosts share container ids, so clearing by id alone
                    # would clear badges on machines nothing was done to.
                    if endpoint_id is not None and \
                            container.get("endpointId") != endpoint_id:
                        continue
                    if container["id"] == container_id and \
                            container["status"] == STATUS_UPDATE_AVAILABLE:
                        container["status"] = STATUS_UP_TO_DATE

    async def check_all(self, notify: bool = True) -> dict:
        """Check every instance. `notify=False` is for a check someone asked
        for from the dashboard: they are looking at the result, so a mail
        saying the same thing is noise. It also leaves the record of what has
        been announced untouched, so the next scheduled check still reports
        anything that is new since the last mail."""
        if self._lock.locked():
            # A check is already running. Its result will be exactly as fresh
            # as one started now, so wait for it instead of scanning every
            # registry a second time.
            async with self._lock:
                return self.snapshot()
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
                if notify:
                    await self._notify_new()
            finally:
                self.checking = False
        return self.snapshot()

    async def _notify_new(self) -> None:
        previous = self._notified
        current: set[tuple] = set()
        events: list[UpdateEvent] = []
        observed: set[int] = set()
        for instance_result in self.results:
            iid = instance_result["instance"]["id"]
            if instance_result.get("error"):
                continue  # not seen this time — says nothing about its updates
            observed.add(iid)
            for stack in instance_result["stacks"]:
                for image in stack["images"]:
                    if image["status"] != STATUS_UPDATE_AVAILABLE:
                        continue
                    key = (iid, stack["id"], image["image"])
                    current.add(key)
                    if key not in previous:
                        events.append(UpdateEvent(
                            instance_name=instance_result["instance"]["name"],
                            stack_name=stack["name"],
                            image=image["image"],
                        ))
            for container in instance_result.get("containers", []):
                if container["status"] != STATUS_UPDATE_AVAILABLE:
                    continue
                # Container ids are only unique within one host. Machines cloned
                # from a template share them, so the environment is part of the
                # identity — without it several hosts collapse into one entry
                # and the mail repeats a line it cannot tell apart.
                key = (iid, "container", container.get("endpointId"),
                       container["id"], container["image"])
                if key in current:
                    continue
                current.add(key)
                if key not in previous:
                    events.append(UpdateEvent(
                        instance_name=instance_result["instance"]["name"],
                        stack_name=container["name"],
                        image=container["image"],
                        environment=container.get("environment"),
                    ))
        # An instance that could not be checked keeps what was known about
        # it: forgetting would re-announce every one of its updates when it
        # comes back. Resolved updates (observed, and gone) are dropped.
        carried = {key for key in previous if key[0] not in observed}
        delivered = True
        if events:
            for notifier in self.notifiers:
                try:
                    await notifier.send(events)
                except Exception:
                    delivered = False
                    logger.exception("Notifier %s failed", type(notifier).__name__)
        if delivered:
            self._notified = current | carried
        else:
            # Nothing new counts as announced; it is tried again next check.
            self._notified = (previous & current) | carried
        self._save_notified()

    async def run_periodic(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:
                logger.exception("Scheduled update check failed")
            await asyncio.sleep(self.interval_hours * 3600)
