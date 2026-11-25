"""One reading of an instance, shared by the dashboard and the update checker."""

import pytest

from app.dashboard import instance_view
from app.inventory import read_inventory
from tests.fakes import FakePortainer

WEB_FILE = "services:\n  web:\n    image: nginx:latest\n  db:\n    image: postgres:16\n"
MOVED = "sha256:" + "c" * 64


def lab() -> FakePortainer:
    """A stack of two services and one container beside it."""
    host = FakePortainer()
    host.add_stack(1, "web", file=WEB_FILE)
    host.add_container(1, "web-web-1", "nginx:latest", stack="web")
    host.add_container(1, "web-db-1", "postgres:16", stack="web")
    host.add_container(1, "adguard", "adguard/adguardhome:latest")
    return host


def names(containers) -> list[str]:
    return sorted(c["Names"][0].lstrip("/") for c in containers)


@pytest.mark.asyncio
async def test_a_stack_owns_its_containers_and_the_rest_stand_alone():
    inventory = await read_inventory(lab())

    (entry,) = inventory.stacks
    assert names(entry.containers) == ["web-db-1", "web-web-1"]
    assert entry.images == ["nginx:latest", "postgres:16"]
    assert [(e, names([c])) for e, c in inventory.standalone] == [(1, ["adguard"])]
    assert inventory.environments == {1: "local"}


@pytest.mark.asyncio
async def test_a_compose_project_named_like_a_stack_elsewhere_is_not_that_stack():
    host = FakePortainer(endpoints={1: "nas", 2: "pi"})
    host.add_stack(1, "web", endpoint_id=1, file=WEB_FILE)
    host.add_container(2, "web-web-1", "nginx:latest", stack="web")

    inventory = await read_inventory(host)

    assert inventory.stacks[0].containers == []
    assert [e for e, _ in inventory.standalone] == [2]


@pytest.mark.asyncio
async def test_a_moved_tag_is_resolved_once_for_everyone():
    """A container whose tag was re-pulled reports a bare image id. The name
    behind it is asked for once, and only for containers that need it."""
    host = lab()
    moved = host.add_container(1, "portainer_agent", MOVED)
    host.container_info[moved["Id"]] = {"Config": {"Image": "portainer/agent:latest"}}

    inventory = await read_inventory(host)

    assert inventory.ref(1, moved) == "portainer/agent:latest"
    plain = inventory.stacks[0].containers[0]
    assert inventory.ref(1, plain) == plain["Image"]
    inspected = [c for c in host.calls if c[0] == "get_container_info"]
    assert inspected == [("get_container_info", 1, moved["Id"])]


@pytest.mark.asyncio
async def test_an_environment_that_cannot_be_read_is_recorded_not_emptied():
    host = FakePortainer(endpoints={1: "nas", 2: "pi"})
    host.add_stack(1, "web", endpoint_id=1, file=WEB_FILE)
    host.add_stack(2, "media", endpoint_id=2, file="services: {tv: {image: jellyfin:latest}}")
    host.add_container(1, "web-web-1", "nginx:latest", stack="web")
    host.failing[("list_containers", 2)] = RuntimeError("agent timed out")

    inventory = await read_inventory(host)

    assert inventory.container_errors == {2: "RuntimeError: agent timed out"}
    assert 2 not in inventory.containers and 1 in inventory.containers
    media = inventory.stacks[1]
    assert media.containers == [] and media.images == ["jellyfin:latest"]


@pytest.mark.asyncio
async def test_stacks_are_still_read_when_the_environments_cannot_be_listed():
    host = lab()
    host.failing["list_endpoints"] = RuntimeError("503")

    inventory = await read_inventory(host)

    assert inventory.environments == {} and "503" in inventory.environments_error
    assert names(inventory.stacks[0].containers) == ["web-db-1", "web-web-1"]


@pytest.mark.asyncio
async def test_an_unreadable_stack_file_falls_back_to_what_the_containers_run():
    host = lab()
    host.failing[("get_stack_file", 1)] = RuntimeError("no file")

    inventory = await read_inventory(host)

    assert inventory.stacks[0].file == ""
    assert sorted(inventory.stacks[0].images) == ["nginx:latest", "postgres:16"]


@pytest.mark.asyncio
async def test_nothing_is_known_when_the_stacks_cannot_be_listed():
    host = lab()
    host.failing["list_stacks"] = RuntimeError("connection refused")

    with pytest.raises(RuntimeError):
        await read_inventory(host)

    view = await instance_view(7, "lab", host)
    assert view["reachable"] is False and "connection refused" in view["error"]
    assert host.reconnected == 1, "the next poll starts from a clean connection"


# --- the rows the page is sent ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_dashboard_says_what_could_not_be_read():
    host = FakePortainer(endpoints={1: "nas", 2: "pi"})
    host.add_stack(2, "media", endpoint_id=2, file="services: {tv: {image: jellyfin:latest}}")
    host.failing[("list_containers", 2)] = RuntimeError("agent timed out")

    view = await instance_view(7, "lab", host)

    assert view["reachable"] is True and view["environments"] == 2
    assert view["environmentErrors"] == {
        "pi": "could not list containers: RuntimeError: agent timed out"}
    (media,) = view["stacks"]
    assert media["unchecked"] is True and media["environment"] == "pi"


@pytest.mark.asyncio
async def test_rows_carry_what_the_page_needs_to_decide_its_buttons():
    """The page never matches on image names or state words: whether a row
    can be stopped, needs the helper, is stopped, or is Restruo itself is
    worked out here."""
    host = FakePortainer()
    host.add_container(1, "portainer", "portainer/portainer-ce:lts")
    agent = host.add_container(1, "portainer_agent", MOVED)
    host.container_info[agent["Id"]] = {"Config": {"Image": "portainer/agent:latest"}}
    host.add_container(1, "restruo", "ghcr.io/jwapps-app/restruo:latest")
    host.add_container(1, "backup", "restic:latest", state="exited", status="Exited (0) 2 hours ago")
    host.add_container(1, "sick", "nginx:latest", status="Up 2 hours (unhealthy)")

    view = await instance_view(7, "lab", host)
    rows = {c["name"]: c for c in view["containers"]}

    def flags(name):
        return {k: rows[name][k] for k in ("helper", "selfCritical", "isSelf", "stopped", "running")}

    assert flags("portainer") == {"helper": "portainer", "selfCritical": True,
                                  "isSelf": False, "stopped": False, "running": True}
    assert rows["portainer_agent"]["image"] == "portainer/agent:latest"
    assert flags("portainer_agent")["helper"] == "agent"
    assert flags("restruo") == {"helper": "", "selfCritical": True,
                                "isSelf": True, "stopped": False, "running": True}
    assert flags("backup") == {"helper": "", "selfCritical": False,
                               "isSelf": False, "stopped": True, "running": False}
    assert flags("sick")["running"] is True and rows["sick"]["down"] is True


@pytest.mark.asyncio
async def test_stack_rows_are_classified_by_what_their_containers_really_run():
    host = FakePortainer()
    host.add_stack(1, "infra", file="services: {agent: {image: 'portainer/agent:latest'}}")
    moved = host.add_container(1, "infra-agent-1", MOVED, stack="infra")
    host.container_info[moved["Id"]] = {"Config": {"Image": "portainer/agent:latest"}}
    host.add_stack(2, "dash", file="services: {r: {image: 'ghcr.io/jwapps-app/restruo:latest'}}")
    host.add_stack(3, "parked", file=WEB_FILE, Status=2)

    view = await instance_view(7, "lab", host)
    infra, dash, parked = view["stacks"]

    assert infra["updateProtected"] is True and infra["selfCritical"] is True
    assert dash["isSelf"] is True and dash["selfCritical"] is True
    assert dash["updateProtected"] is False
    assert parked["stopped"] is True and parked["running"] is False
    assert infra["stopped"] is False and infra["running"] is True
