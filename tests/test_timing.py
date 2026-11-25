"""The deadlines depend on each other. This is where that is checked."""

from app import timing


def test_a_retired_client_outlives_what_may_still_be_using_it():
    assert timing.RETIRE_AFTER_SECONDS > timing.HELPER_TIMEOUT_SECONDS
    assert timing.RETIRE_AFTER_SECONDS > timing.DEPLOY_TIMEOUT_SECONDS
    assert timing.POOL_RETIRE_SECONDS > timing.REDEPLOY_TIMEOUT


def test_the_page_waits_longer_than_any_job_and_the_record_outlives_the_page():
    assert timing.PAGE_JOB_WAIT_SECONDS > timing.HELPER_TIMEOUT_SECONDS
    assert timing.PAGE_JOB_WAIT_SECONDS > timing.DEPLOY_TIMEOUT_SECONDS + timing.REDEPLOY_TIMEOUT
    assert timing.JOB_RETENTION_SECONDS > timing.PAGE_JOB_WAIT_SECONDS


def test_a_quick_answer_is_quicker_than_a_proxy_gives_up():
    assert timing.JOB_SYNC_WAIT_SECONDS < 60


def test_the_page_is_told_how_long_to_wait(client):
    config = client.get("/api/ui-config", auth=("admin", "hunter2")).json()
    assert config["jobWaitSeconds"] == timing.PAGE_JOB_WAIT_SECONDS
