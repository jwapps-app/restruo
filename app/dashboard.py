"""The rows the dashboard shows, built from an Inventory.

Everything the page needs to decide how a row looks and what its buttons do
is computed here and sent as a field: whether it can be stopped, whether it
needs the helper, whether it is stopped, whether it is Restruo itself. The
page renders those; it never matches on image names or state words.
"""

import logging

from . import health
from .errors import describe
from .inventory import Inventory, StackEntry, read_inventory
from .portainer import (
    STACK_INACTIVE,
    PortainerClient,
    cannot_recreate_image,
    container_is_down,
    container_name,
    helper_kind,
    is_restruo_image,
    is_self_critical_image,
    normalize_container,
    normalize_stack,
)

logger = logging.getLogger("restruo.dashboard")


def stack_row(entry: StackEntry, inventory: Inventory) -> dict:
    stack = entry.stack
    endpoint_id = stack["EndpointId"]
    # What its containers really run, and what its file says it uses.
    images = [inventory.ref(endpoint_id, c) for c in entry.containers] + entry.images
    row = normalize_stack(stack, entry.images)
    row["containersTotal"] = len(entry.containers)
    row["downNames"] = [container_name(c) for c in entry.containers if container_is_down(c)]
    # Unknown is not the same as fine.
    row["unchecked"] = endpoint_id not in inventory.containers
    row["environment"] = inventory.environments.get(endpoint_id, "")
    row["stopped"] = stack.get("Status") == STACK_INACTIVE
    row["running"] = not row["stopped"]
    # Stacks running Portainer or Restruo can't be stopped from here…
    row["selfCritical"] = any(is_self_critical_image(i) for i in images)
    # …and one running Portainer or an agent can't be redeployed from here.
    row["updateProtected"] = any(cannot_recreate_image(i) for i in images)
    # Updating this one restarts the dashboard itself.
    row["isSelf"] = any(is_restruo_image(i) for i in images)
    return row


def container_row(endpoint_id: int, container: dict, inventory: Inventory) -> dict:
    image = inventory.ref(endpoint_id, container)
    row = normalize_container(container, endpoint_id)
    row["image"] = image
    row["environment"] = inventory.environments.get(endpoint_id, "")
    row["stopped"] = health.is_stopped(container)
    row["running"] = (container.get("State") or "").lower() == "running"
    # "agent", "portainer" or "": replaced by the helper rather than recreated.
    row["helper"] = helper_kind(image)
    row["selfCritical"] = is_self_critical_image(image)
    row["isSelf"] = is_restruo_image(image)
    return row


def environment_errors(inventory: Inventory) -> dict[str, str]:
    """What could not be read, by environment name — reported as such, never
    as "nothing there"."""
    errors: dict[str, str] = {}
    if inventory.environments_error:
        errors["(all)"] = f"could not list environments: {inventory.environments_error}"
    for endpoint_id, reason in inventory.container_errors.items():
        errors[inventory.environment_name(endpoint_id)] = f"could not list containers: {reason}"
    return errors


async def instance_view(iid: int, name: str, client: PortainerClient) -> dict:
    """One instance's section of the dashboard."""
    view = {
        "instance": {"id": iid, "name": name},
        "stacks": [],
        "containers": [],
        "reachable": True,
        "error": None,
    }
    try:
        inventory = await read_inventory(client)
    except Exception as exc:
        view.update(reachable=False, error=describe(exc))
        logger.warning("Instance %r unreachable: %s", name, describe(exc))
        # Whatever went wrong, start the next poll from a clean connection and
        # a fresh login — the same reset that re-saving the instance performs.
        await client.reconnect()
        return view
    # Environment names are kept so each row can say where it actually runs.
    view["environments"] = len(inventory.environments)
    view["environmentErrors"] = environment_errors(inventory)
    view["stacks"] = [stack_row(entry, inventory) for entry in inventory.stacks]
    view["containers"] = [
        container_row(endpoint_id, container, inventory)
        for endpoint_id, container in inventory.standalone
    ]
    return view
