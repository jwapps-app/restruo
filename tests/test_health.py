"""One reading of "is this container working", for each source that says."""

import pytest

from app import health


def inspect(running=True, restarting=False, code=0, check=None, restarts=0):
    state = {"Running": running, "Restarting": restarting, "ExitCode": code}
    if check:
        state["Health"] = {"Status": check}
    return {"State": state, "RestartCount": restarts}


@pytest.mark.parametrize("info, verdict", [
    (inspect(), health.WORKING),
    (inspect(check="healthy"), health.WORKING),
    (inspect(check="starting"), health.STARTING),
    (inspect(check="unhealthy"), health.UNHEALTHY),
    (inspect(running=False, code=1), health.EXITED),
    (inspect(running=False, code=0), health.EXITED),          # whether that is fine is the caller's call
    (inspect(running=True, restarting=True), health.RESTARTING),
    (inspect(running=False, restarting=True), health.RESTARTING),
    ({}, health.EXITED),
])
def test_an_inspected_container_is_judged_one_way(info, verdict):
    assert health.judge(info) == verdict


def test_exit_code_and_restarts_are_read_separately():
    assert health.exit_code(inspect(running=False, code=137)) == 137
    assert health.has_restarted(inspect(restarts=3)) and not health.has_restarted(inspect())


@pytest.mark.parametrize("state, status, down, working, stopped", [
    ("running", "Up 2 hours", False, True, False),
    ("running", "Up 2 hours (healthy)", False, True, False),
    ("running", "Up 2 hours (unhealthy)", True, False, False),
    ("exited", "Exited (0) 3 minutes ago", True, True, True),     # a one-shot job that finished
    ("exited", "Exited (1) 3 minutes ago", True, False, True),
    ("restarting", "Restarting (1) 2 seconds ago", True, False, False),
    ("created", "Created", True, False, True),
    ("dead", "Dead", True, False, True),
    ("paused", "Up 2 hours (Paused)", True, False, False),
])
def test_a_listed_container_is_read_one_way(state, status, down, working, stopped):
    container = {"State": state, "Status": status}
    assert health.container_is_down(container) is down
    assert health.container_is_working(container) is working
    assert health.is_stopped(container) is stopped
