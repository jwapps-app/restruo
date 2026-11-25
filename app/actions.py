"""The things Restruo does to a stack or a container.

Each action checks what must be checked first, takes the target in
Operations so nothing collides with it, does the work through one Portainer
client, and answers with the result shape from operations.Timed. A refusal is
raised as errors.Refused (or a subclass) with words for the person who asked.
Nothing here knows about HTTP requests.
"""

import asyncio
from dataclasses import dataclass

from . import health, helper_runner, outcome
from .errors import NotFound, PortainerError, Refused, UpstreamFailed, describe
from .operations import (
    Job,
    Operations,
    Timed,
    container_key,
    helper_key,
    prune_key,
    stack_key,
)
from .portainer import (
    STACK_INACTIVE,
    PortainerClient,
    cannot_recreate_image,
    container_name,
    extract_images,
    helper_kind,
    interpolate,
    is_self_critical_image,
    profiled_images,
    registry_id_for,
    resolve_image_name,
    stack_containers,
    stack_env,
    stack_is_well_formed,
)

STOPPED_STACK_PULLS = 2     # images pulled at once for a stopped stack


def stack_name(stack: dict) -> str:
    return stack.get("Name") or f"stack {stack.get('Id')}"


# --- finding the target -------------------------------------------------------


async def fetch_stack(client: PortainerClient, stack_id: int) -> dict:
    """The stack as Portainer has it now, so Env and EndpointId are current
    when it is acted on."""
    try:
        stack = await client.get_stack(stack_id)
    except PortainerError as exc:
        if exc.status_code == 404:
            raise NotFound(f"No stack with id {stack_id} on this instance")
        raise UpstreamFailed(f"Could not fetch stack: {exc.message}")
    except Exception as exc:
        raise UpstreamFailed(f"Could not fetch stack: {describe(exc)}")
    if not isinstance(stack, dict) or not stack_is_well_formed(stack):
        raise UpstreamFailed(f"Could not fetch stack: Portainer's answer for stack "
                             f"{stack_id} has no usable id or environment")
    return stack


@dataclass(frozen=True)
class ContainerTarget:
    endpoint_id: int
    id: str
    name: str
    # The reference it was created from, resolved past a moved tag.
    image: str
    stopped: bool


async def find_container(client: PortainerClient, cid: str, endpoint_id: int) -> dict:
    """Locate a container in one named environment.

    Container ids are unique per host, not per Portainer: machines cloned from
    a template carry the same ids. Scanning environments for a bare id would
    act on whichever host happens to come first — a different machine than
    the row that was clicked — so the environment is never optional.
    """
    for container in await client.list_containers(endpoint_id):
        if container.get("Id") == cid:
            return container
    raise NotFound(f"No container {cid[:12]} in environment {endpoint_id}")


async def locate_container(
    client: PortainerClient, cid: str, endpoint_id: int
) -> ContainerTarget:
    try:
        container = await find_container(client, cid, endpoint_id)
        image = await resolve_image_name(client, endpoint_id, container)
    except Refused:
        raise
    except Exception as exc:
        raise UpstreamFailed(f"Could not find container: {describe(exc)}")
    return ContainerTarget(
        endpoint_id=endpoint_id, id=cid, name=container_name(container),
        image=image, stopped=health.is_stopped(container),
    )


async def stack_image_names(client: PortainerClient, stack: dict, verb: str) -> list[str]:
    """What the stack's containers actually run, resolved past a moved tag.

    This feeds the guards that keep Portainer and its agents from being
    stopped or redeployed through themselves. A guard that cannot look must
    not pass — so a failed listing refuses the action rather than allowing it.
    """
    endpoint_id = stack["EndpointId"]
    try:
        own = stack_containers(stack, await client.list_containers(endpoint_id))
        return [await resolve_image_name(client, endpoint_id, c) for c in own]
    except Exception as exc:
        raise UpstreamFailed(
            f"Couldn't confirm what “{stack_name(stack)}” runs ({describe(exc)}) — "
            f"refusing to {verb} it until that can be checked."
        )


# --- updating a stack -----------------------------------------------------------


async def update_running_stack(client: PortainerClient, stack: dict) -> dict:
    """Repull and redeploy, then watch the deploy until it has settled."""
    timed = Timed(stack_name(stack))
    containers = await outcome.stack_containers_now(client, stack)
    before = outcome.fingerprint(containers or [])
    try:
        await client.update_stack(stack)
    except Exception as exc:
        return timed.fail(describe(exc))
    return timed.result(**await outcome.await_deploy(client, stack, before))


async def update_stopped_stack(client: PortainerClient, stack: dict) -> dict:
    """Update a stack that is stopped, and leave it stopped.

    A redeploy is the wrong tool here: Portainer brings the stack up and marks
    it active, so something stopped on purpose is running again after every
    update. Redeploying and then stopping it again would still start it for a
    while — and a stack may be stopped precisely because it must not run.

    So nothing is started at all. The stack's images are pulled on its
    environment; the tags then point at the new images, and that is what the
    stack starts on, whenever it is next started.
    """
    timed = Timed(stack_name(stack))
    try:
        content = await client.get_stack_file(stack["Id"])
    except Exception as exc:
        return timed.fail(f"Could not read the stack file: {describe(exc)}. "
                          "Nothing was changed; the stack is still stopped.")
    env = stack_env(stack)
    behind_profile = profiled_images(content, env)
    images: list[str] = []
    unresolved: list[str] = []
    for raw in extract_images(content):
        if raw in behind_profile:
            continue  # never deployed, so nothing to keep current
        resolved = interpolate(raw, env)
        if resolved is None:
            unresolved.append(raw)
        elif resolved not in images:
            images.append(resolved)
    if not images:
        return timed.fail("No image could be read from the stack file, so there was "
                          "nothing to pull. The stack is still stopped.")

    try:
        registries = await client.list_registries()
    except Exception:
        registries = []  # anonymous pulls still work for public images
    gate = asyncio.Semaphore(STOPPED_STACK_PULLS)

    async def pull(image: str) -> str | None:
        async with gate:
            try:
                await client.pull_image(
                    stack["EndpointId"], image, registry_id_for(image, registries)
                )
                return None
            except Exception as exc:
                return f"{image} ({describe(exc)})"

    failures = [f for f in await asyncio.gather(*(pull(i) for i in images)) if f]
    pulled = len(images) - len(failures)
    if failures or unresolved:
        problems = [f"could not pull {f}" for f in failures] + [
            f"could not resolve {u} — it uses a variable Portainer has no value for"
            for u in unresolved
        ]
        return timed.fail(f"Pulled {pulled} of {len(images) + len(unresolved)} images; "
                          f"{'; '.join(problems)}. The stack is still stopped.")
    note = (" Its compose file was not re-read from git — that happens when a running "
            "stack is redeployed.") if stack.get("GitConfig") else ""
    return timed.ok(f"Pulled {pulled} image{'' if pulled == 1 else 's'}. The stack was "
                    f"stopped and stays stopped — the new "
                    f"{'image is' if pulled == 1 else 'images are'} used the next "
                    f"time it is started.{note}")


# --- cleaning up ------------------------------------------------------------------


async def prune_endpoints(
    client: PortainerClient, endpoints: list[dict], *,
    images: bool, all_images: bool, networks: bool, volumes: bool,
) -> dict:
    """Remove unused Docker leftovers on each environment, carrying on past
    one that fails. Every failure is in the summary's `errors`."""
    summary = {
        "ok": True, "spaceReclaimed": 0,
        "images": 0, "networks": 0, "volumes": 0, "errors": [],
    }
    for endpoint in endpoints:
        endpoint_id = endpoint["Id"]
        if images:
            try:
                pruned = await client.prune_images(endpoint_id, all_unused=all_images)
                summary["images"] += len(pruned.get("ImagesDeleted") or [])
                summary["spaceReclaimed"] += pruned.get("SpaceReclaimed") or 0
            except Exception as exc:
                summary["errors"].append(f"images: {describe(exc)}")
        if networks:
            try:
                removed, failures = await client.remove_unused_networks(endpoint_id)
                summary["networks"] += removed
                summary["errors"].extend(f"networks: {f}" for f in failures)
            except Exception as exc:
                summary["errors"].append(f"networks: {describe(exc)}")
        if volumes:
            try:
                pruned = await client.prune_volumes(endpoint_id)
                summary["volumes"] += len(pruned.get("VolumesDeleted") or [])
                summary["spaceReclaimed"] += pruned.get("SpaceReclaimed") or 0
            except Exception as exc:
                summary["errors"].append(f"volumes: {describe(exc)}")
    summary["ok"] = not summary["errors"]
    return summary


# --- the actions --------------------------------------------------------------------


class Actions:
    """Everything a button does. `checker` is told when an update succeeded,
    so its badge clears without waiting for the next registry check."""

    def __init__(self, ops: Operations, checker, helper_image: str):
        self.ops = ops
        self.checker = checker
        self.helper_image = helper_image

    async def update_stack(self, client: PortainerClient, iid: int, stack: dict) -> Job:
        name = stack_name(stack)
        images = await stack_image_names(client, stack, "redeploy")
        if any(cannot_recreate_image(i) for i in images):
            raise Refused(
                f"“{name}” runs Portainer or a Portainer agent. "
                "Redeploying it would stop the container carrying the command, so "
                "the redeploy could never finish. Update it from that host instead."
            )

        async def work() -> dict:
            if stack.get("Status") == STACK_INACTIVE:
                result = await update_stopped_stack(client, stack)
            else:
                result = await update_running_stack(client, stack)
            if result["ok"]:
                self.checker.mark_updated(iid, stack_id=stack["Id"])
            return result

        return await self.ops.run(stack_key(iid, stack["EndpointId"], stack["Id"]), work)

    async def set_stack_state(
        self, client: PortainerClient, iid: int, stack: dict, running: bool
    ) -> dict:
        """Start or stop a stack."""
        timed = Timed(stack_name(stack))
        if not running:
            images = await stack_image_names(client, stack, "stop")
            if any(is_self_critical_image(i) for i in images):
                raise Refused(
                    f"“{timed.name}” runs Portainer or Restruo itself — stopping it from "
                    "here would cut off the connection needed to start it again."
                )
        try:
            async with self.ops.exclusive(stack_key(iid, stack["EndpointId"], stack["Id"])):
                await client.set_stack_state(stack["Id"], stack["EndpointId"], running=running)
        except Refused:
            raise
        except Exception as exc:
            return timed.fail(describe(exc))
        return timed.ok("Started." if running else "Stopped.")

    async def update_container(
        self, client: PortainerClient, iid: int, target: ContainerTarget
    ) -> Job:
        """Repull + recreate a standalone container."""
        timed = Timed(target.name)

        def succeeded() -> None:
            self.checker.mark_updated(
                iid, container_id=target.id, endpoint_id=target.endpoint_id
            )

        kind = helper_kind(target.image)
        if kind:
            # Portainer's own recreate stops the container that is carrying the
            # command, so the create that should follow is never sent. Hand the
            # job to a helper the Docker daemon runs, which outlives it.
            async def helper_work() -> dict:
                result = await helper_runner.replace_with_helper(
                    client, target.endpoint_id, target.id, target.name, self.helper_image
                )
                if result["ok"]:
                    succeeded()
                return timed.result(**result)

            return await self.ops.run(
                helper_key(kind, iid, target.endpoint_id, target.id), helper_work
            )

        async def work() -> dict:
            try:
                await client.recreate_container(target.endpoint_id, target.id)
            except Exception as exc:
                return timed.fail(describe(exc))
            if target.stopped:
                # Portainer's recreate always starts what it creates, whatever
                # state the original was in. One that was stopped is stopped again.
                result = {"ok": True, "message": await outcome.stop_again(
                    client, target.endpoint_id, target.name)}
            else:
                # Portainer answers as soon as the new container has been
                # started. Whether it then stays up is a separate question,
                # and the one that matters.
                result = await outcome.await_container(
                    client, target.endpoint_id, target.name)
            if result["ok"]:
                succeeded()
            return timed.result(**result)

        return await self.ops.run(container_key(iid, target.endpoint_id, target.id), work)

    async def set_container_state(
        self, client: PortainerClient, iid: int, target: ContainerTarget, running: bool
    ) -> dict:
        """Start or stop a standalone container."""
        timed = Timed(target.name)
        if not running and is_self_critical_image(target.image):
            raise Refused(
                f"“{target.name}” is Portainer or Restruo itself — stopping it from here "
                "would cut off the connection needed to start it again."
            )
        try:
            async with self.ops.exclusive(container_key(iid, target.endpoint_id, target.id)):
                await client.set_container_state(
                    target.endpoint_id, target.id, running=running)
        except Refused:
            raise
        except Exception as exc:
            return timed.fail(describe(exc))
        return timed.ok("Started." if running else "Stopped.")

    async def prune(
        self, client: PortainerClient, iid: int, *,
        images: bool, all_images: bool, networks: bool, volumes: bool,
    ) -> dict:
        """Remove unused Docker leftovers on every environment of one instance."""
        try:
            endpoints = await client.list_endpoints()
        except Exception as exc:
            raise UpstreamFailed(f"Could not list environments: {describe(exc)}")
        async with self.ops.exclusive(prune_key(iid)):
            return await prune_endpoints(
                client, endpoints, images=images, all_images=all_images,
                networks=networks, volumes=volumes,
            )
