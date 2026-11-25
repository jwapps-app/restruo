"""What happened to Arcane, as tests.

A standalone container was updated. The new image refused the old settings
("legacy environment variables are no longer read") and exited four seconds
after starting — by which time Restruo had already said "Repulled and
recreated", because Portainer answers as soon as the container is started.
Six seconds later a Clean up ran. Docker's network prune treats a network as
unused when nothing *running* is attached, so it deleted the crashed
container's network, and from then on the container could not be started at
all: "network … not found". Both halves were confirmed against a real Engine.
"""
import pytest

from app import timing
from app.outcome import await_container
from app.portainer import networks_safe_to_remove


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(timing, "CONTAINER_POLL_SECONDS", 0.001)


class Host:
    """A recreated container whose inspect result follows a script; the last
    entry repeats."""

    def __init__(self, states, logs="", name="arcane"):
        self.states, self.calls, self.logs, self.name = states, 0, logs, name

    async def list_containers(self, endpoint_id):
        return [{"Id": "e03bb2d1d047", "Names": [f"/{self.name}"]}]

    async def get_container_info(self, endpoint_id, cid):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        if isinstance(state, Exception):
            raise state
        return state

    async def container_logs(self, endpoint_id, cid):
        return self.logs


def up(health=None, restarts=0):
    state = {"Running": True, "Restarting": False}
    if health:
        state["Health"] = {"Status": health}
    return {"State": state, "RestartCount": restarts}


def exited(code):
    return {"State": {"Running": False, "Restarting": False, "ExitCode": code}, "RestartCount": 0}


ARCANE_LOG = ('2026/11/14 22:25:19 \x1b[31mERROR\x1b[0m Failed to run Arcane error="legacy environment '
              'variables are no longer read: rename APP_URL to ARCANE_APP_URL"\n\n')


@pytest.mark.asyncio
async def test_a_container_that_exits_is_a_failure_with_its_own_explanation():
    outcome = await await_container(Host([up(), exited(1)], ARCANE_LOG), 18, "arcane")
    assert outcome["ok"] is False
    assert "exited right away (exit code 1)" in outcome["message"]
    assert "rename APP_URL to ARCANE_APP_URL" in outcome["message"]
    assert "\x1b" not in outcome["message"], "colour codes stripped"


@pytest.mark.asyncio
async def test_a_crash_loop_is_a_failure_even_while_it_reads_as_running():
    """restart: unless-stopped — between crashes the container is 'running'."""
    outcome = await await_container(Host([up(restarts=3)], ARCANE_LOG), 18, "arcane")
    assert outcome["ok"] is False and "keeps crashing and restarting" in outcome["message"]


@pytest.mark.asyncio
async def test_a_container_that_stays_up_is_a_success():
    host = Host([up()])
    outcome = await await_container(host, 18, "arcane")
    assert outcome == {"ok": True, "message": "Repulled and recreated — running."}
    assert host.calls == timing.CONTAINER_STABLE_POLLS, "watched for several polls, not one"


@pytest.mark.asyncio
async def test_unhealthy_is_a_failure_and_a_passing_healthcheck_is_waited_for():
    sick = await await_container(Host([up("starting"), up("unhealthy")]), 18, "arcane")
    assert sick["ok"] is False and "health check" in sick["message"]
    well = await await_container(Host([up("starting"), up("starting"), up("healthy")]), 18, "arcane")
    assert well["ok"] is True


@pytest.mark.asyncio
async def test_a_slow_healthcheck_is_not_called_a_failure():
    outcome = await await_container(Host([up("starting")]), 18, "arcane")
    assert outcome["ok"] is True and "health check had not finished" in outcome["message"]


@pytest.mark.asyncio
async def test_a_one_shot_container_that_finishes_cleanly_is_fine():
    outcome = await await_container(Host([exited(0)]), 18, "arcane")
    assert outcome["ok"] is True and "finished cleanly" in outcome["message"]


@pytest.mark.asyncio
async def test_failed_polls_are_ignored_and_a_missing_container_is_reported():
    flaky = await await_container(Host([RuntimeError("proxy"), up()]), 18, "arcane")
    assert flaky["ok"] is True
    gone = await await_container(Host([up()], name="something-else"), 18, "arcane")
    assert gone["ok"] is False and "could not be found" in gone["message"]


# --- Clean up -----------------------------------------------------------------

NETWORKS = [
    {"Id": "d45d902b605f", "Name": "arcane_default", "Scope": "local"},
    {"Id": "56d273e365bf", "Name": "komodo_default", "Scope": "local"},
    {"Id": "aaaa00000001", "Name": "leftover_default", "Scope": "local"},
    {"Id": "ca6fae574345", "Name": "bridge", "Scope": "local"},
    {"Id": "e6e5fb94441d", "Name": "host", "Scope": "local"},
    {"Id": "016cf3ed94ce", "Name": "none", "Scope": "local"},
    {"Id": "bbbb00000002", "Name": "ingress", "Scope": "swarm", "Ingress": True},
    {"Id": "cccc00000003", "Name": "cluster_net", "Scope": "swarm"},
]


def test_the_network_of_a_crashed_container_is_kept():
    containers = [
        {"Id": "e03bb2d1d047", "State": "exited", "HostConfig": {"NetworkMode": "arcane_default"},
         "NetworkSettings": {"Networks": {"arcane_default": {"NetworkID": "d45d902b605f"}}}},
        {"Id": "111111111111", "State": "running", "HostConfig": {"NetworkMode": "komodo_default"},
         "NetworkSettings": {"Networks": {"komodo_default": {"NetworkID": "56d273e365bf"}}}},
    ]
    assert [n["Name"] for n in networks_safe_to_remove(NETWORKS, containers)] == ["leftover_default"]


def test_a_network_is_kept_when_only_its_name_or_only_its_id_is_referenced():
    by_mode_only = [{"Id": "x" * 12, "HostConfig": {"NetworkMode": "leftover_default"}, "NetworkSettings": {}}]
    assert networks_safe_to_remove(NETWORKS[2:3], by_mode_only) == []
    by_id_only = [{"Id": "y" * 12, "HostConfig": {},
                   "NetworkSettings": {"Networks": {"renamed": {"NetworkID": "aaaa00000001"}}}}]
    assert networks_safe_to_remove(NETWORKS[2:3], by_id_only) == []


def test_builtin_and_cluster_networks_are_never_candidates():
    names = [n["Name"] for n in networks_safe_to_remove(NETWORKS, [])]
    assert names == ["arcane_default", "komodo_default", "leftover_default"]


@pytest.mark.asyncio
async def test_nothing_is_removed_when_the_containers_cannot_be_listed():
    from app.instances import InstanceRecord
    from app.portainer import PortainerClient, PortainerError
    import httpx
    deleted = []

    def handler(request):
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(502, json={"message": "Proxy failure"})
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return httpx.Response(200, json=NETWORKS)

    client = PortainerClient(InstanceRecord(id=1, name="p", base_url="https://p.test", auth_type="api_key", api_key="k"),
                             transport=httpx.MockTransport(handler))
    with pytest.raises(PortainerError):
        await client.remove_unused_networks(18)
    assert deleted == []
