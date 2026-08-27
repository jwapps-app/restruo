"""Container ids are unique per host, not per Portainer.

Machines cloned from a template keep the container ids that existed at clone
time, so one Portainer can legitimately report the same id in several
environments. Treating the id as unique made the mail repeat a line it could
not tell apart, made every clone show the first host's status, and — worst —
pointed start/stop/update at whichever environment happened to be listed first.
"""
import httpx
import pytest

from app.notifiers import UpdateEvent, compose_body
from app.updates import UpdateChecker

SHARED_ID = "3c8646c2b71b" + "0" * 52


def test_email_distinguishes_clones_by_environment():
    events = [
        UpdateEvent("Proxmox", "portainer_agent", "portainer/agent:latest", "docker-audio"),
        UpdateEvent("Proxmox", "portainer_agent", "portainer/agent:latest", "docker-vault"),
    ]
    body = compose_body(events)
    assert "portainer_agent [docker-audio] — portainer/agent:latest" in body
    assert "portainer_agent [docker-vault] — portainer/agent:latest" in body


def test_identical_rows_notify_once_each_not_once_per_duplicate():
    """Six clones must produce six distinct lines, not six copies of one."""
    checker = UpdateChecker.__new__(UpdateChecker)
    checker._notified = set()
    checker.notifiers = []
    checker.results = [{
        "instance": {"id": 5, "name": "Proxmox"},
        "stacks": [],
        "containers": [
            {"id": SHARED_ID, "name": "portainer_agent", "endpointId": eid,
             "environment": env, "image": "portainer/agent:latest",
             "status": "update-available"}
            for eid, env in [(11, "docker-audio"), (12, "docker-immich"),
                             (13, "docker-apps"), (14, "docker-vault")]
        ],
    }]

    import asyncio
    captured = []

    class Capture:
        async def send(self, events):
            captured.extend(events)

    checker.notifiers = [Capture()]
    asyncio.run(checker._notify_new())

    assert len(captured) == 4
    assert sorted(e.environment for e in captured) == [
        "docker-apps", "docker-audio", "docker-immich", "docker-vault",
    ]


@pytest.mark.asyncio
async def test_action_targets_the_environment_it_was_clicked_in():
    """The dangerous one: without the endpoint, the first match wins and the
    wrong machine gets restarted."""
    from app.main import _find_container
    from app.instances import InstanceRecord
    from app.portainer import PortainerClient

    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": e, "Name": f"env{e}"} for e in (10, 11, 12)])
        if "/docker/containers/json" in path:
            eid = int(path.split("/api/endpoints/")[1].split("/")[0])
            asked.append(eid)
            return httpx.Response(200, json=[{
                "Id": SHARED_ID, "Names": [f"/agent-on-{eid}"],
                "Image": "portainer/agent:latest", "State": "running",
            }])
        return httpx.Response(404, json={"message": "not found"})

    client = PortainerClient(
        InstanceRecord(id=5, name="Proxmox", base_url="https://p.test:9443",
                       auth_type="api_key", api_key="k"),
        transport=httpx.MockTransport(handler),
    )

    endpoint_id, container = await _find_container(client, SHARED_ID, endpoint_id=12)

    assert endpoint_id == 12
    assert container["Names"] == ["/agent-on-12"]
    assert 10 not in asked, "must not touch other environments"
