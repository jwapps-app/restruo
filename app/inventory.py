"""One reading of what a Portainer instance runs.

The dashboard and the update checker both need the same picture: which
stacks exist, which containers belong to each, what image every container
was created from, and which containers stand alone. read_inventory() is the
only place that walks an instance to build it, so a rule about what runs
where is written once.

What could not be read is recorded as such — never as "nothing there".
"""

import asyncio
from dataclasses import dataclass, field

from .errors import describe
from .portainer import (
    PortainerClient,
    resolve_image_name,
    stack_containers,
    stack_images,
    stack_names_by_endpoint,
    standalone_containers,
)

# Thirty stacks at once is thirty file fetches in the same instant at one
# Portainer; a few at a time is plenty.
INVENTORY_CONCURRENCY = 6


@dataclass
class StackEntry:
    stack: dict                 # the stack object, as Portainer returned it
    containers: list[dict]      # its own; empty when its environment could not be read
    file: str                   # its compose file; "" when that could not be read
    images: list[str]           # what it uses, resolved as far as can be managed


@dataclass
class Inventory:
    stacks: list[StackEntry]
    # Environments as Portainer listed them, id -> name. One Portainer can
    # manage several (agents, remote hosts).
    environments: dict[int, str]
    # Why the environments could not be listed, when they could not.
    environments_error: str | None
    # Containers of each environment that could be read…
    containers: dict[int, list[dict]]
    # …and why, for each one that could not.
    container_errors: dict[int, str]
    # Containers outside any Portainer stack: (environment id, container).
    standalone: list[tuple[int, dict]]
    _refs: dict[tuple[int, str], str] = field(default_factory=dict, repr=False)

    def ref(self, endpoint_id: int, container: dict) -> str:
        """The image reference this container was created from. A container
        whose tag has since been re-pulled reports a bare image id; this is
        the name behind it, which is what display, update checks and guards
        all go by."""
        fallback = container.get("Image") or ""
        return self._refs.get((endpoint_id, container.get("Id")), fallback)

    def environment_name(self, endpoint_id: int) -> str:
        return self.environments.get(endpoint_id) or f"env {endpoint_id}"


async def read_inventory(client: PortainerClient) -> Inventory:
    """Walk one instance. Raises if its stacks cannot be listed — then nothing
    is known. Everything after that degrades piece by piece instead."""
    stacks = await client.list_stacks()

    environments: dict[int, str] = {}
    environments_error: str | None = None
    try:
        for endpoint in await client.list_endpoints():
            environments[endpoint["Id"]] = endpoint.get("Name") or f"env {endpoint['Id']}"
    except Exception as exc:
        environments_error = describe(exc)

    # Every environment Portainer lists, plus any a stack points at: a stack
    # is still worth reading when the environment listing failed.
    endpoint_ids = list(dict.fromkeys([*environments, *(s["EndpointId"] for s in stacks)]))

    async def containers_of(endpoint_id: int) -> tuple[int, list[dict] | None, str | None]:
        try:
            return endpoint_id, await client.list_containers(endpoint_id), None
        except Exception as exc:
            return endpoint_id, None, describe(exc)

    # Environments are independent hosts; asking them one after another
    # makes everything wait on the slowest agent times the number of agents.
    containers: dict[int, list[dict]] = {}
    container_errors: dict[int, str] = {}
    for endpoint_id, listed, error in await asyncio.gather(
        *(containers_of(e) for e in endpoint_ids)
    ):
        if listed is not None:
            containers[endpoint_id] = listed
        else:
            container_errors[endpoint_id] = error or "unknown error"

    gate = asyncio.Semaphore(INVENTORY_CONCURRENCY)

    async def file_of(stack: dict) -> str:
        async with gate:
            try:
                return await client.get_stack_file(stack["Id"])
            except Exception:
                return ""

    async def ref_of(endpoint_id: int, container: dict) -> tuple[tuple[int, str], str]:
        async with gate:
            return ((endpoint_id, container.get("Id")),
                    await resolve_image_name(client, endpoint_id, container))

    files, refs = await asyncio.gather(
        asyncio.gather(*(file_of(s) for s in stacks)),
        asyncio.gather(*(
            ref_of(endpoint_id, c)
            for endpoint_id, listed in containers.items()
            for c in listed
            # Only a bare image id needs asking about; a name is already the answer.
            if (c.get("Image") or "").startswith("sha256:")
        )),
    )

    entries = []
    for stack, file in zip(stacks, files):
        own = stack_containers(stack, containers.get(stack["EndpointId"], []))
        entries.append(StackEntry(stack, own, file, stack_images(stack, file, own)))

    names_by_endpoint = stack_names_by_endpoint(stacks)
    standalone = [
        (endpoint_id, c)
        for endpoint_id, listed in containers.items()
        for c in standalone_containers(listed, names_by_endpoint.get(endpoint_id, set()))
    ]
    return Inventory(
        stacks=entries,
        environments=environments,
        environments_error=environments_error,
        containers=containers,
        container_errors=container_errors,
        standalone=standalone,
        _refs=dict(refs),
    )
