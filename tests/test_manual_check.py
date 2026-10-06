"""A check asked for by hand does not send mail.

Whoever pressed Refresh is looking at the result; a mail repeating it is
noise. The scheduled check still reports anything new since the last mail.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.updates import UpdateChecker


class Capture:
    def __init__(self):
        self.sent = []

    async def send(self, events):
        self.sent.append(list(events))


def checker_with_one_update():
    capture = Capture()
    checker = UpdateChecker(lambda: [], registry=None, interval_hours=6, notifiers=[capture])

    # No instances to scan, so plant the finding a scan would have produced.
    original = checker._notify_new

    async def notify_with_finding():
        checker.results = [{
            "instance": {"id": 5, "name": "Proxmox"},
            "stacks": [{"id": 1, "name": "quaero", "updatesAvailable": 1, "images": [
                {"image": "ghcr.io/jwapps-app/theology-rag-web:latest",
                 "status": "update-available"}]}],
            "containers": [],
        }]
        await original()

    checker._notify_new = notify_with_finding
    return checker, capture


@pytest.mark.asyncio
async def test_manual_check_sends_nothing_and_remembers_nothing():
    checker, capture = checker_with_one_update()
    await checker.check_all(notify=False)
    assert capture.sent == []
    assert checker._notified == set(), "the finding is still unannounced"


@pytest.mark.asyncio
async def test_scheduled_check_after_a_manual_one_still_reports_it_once():
    checker, capture = checker_with_one_update()
    await checker.check_all(notify=False)
    await checker.check_all()
    assert len(capture.sent) == 1 and capture.sent[0][0].stack_name == "quaero"
    await checker.check_all()
    assert len(capture.sent) == 1, "and not again"


def test_the_refresh_endpoint_asks_for_a_silent_check(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("RESTRUO_USERNAME", "admin")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    for attr in ("config", "store"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)
    with TestClient(app) as client:
        seen = {}

        async def spy(notify=True):
            seen["notify"] = notify
            return {"checkedAt": None, "checking": False, "instances": []}

        app.state.checker.check_all = spy
        assert client.post("/api/check-updates", auth=("admin", "hunter2")).status_code == 200
        assert seen == {"notify": False}
