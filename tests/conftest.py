"""Fixtures shared by every test file."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

BASIC = ("admin", "hunter2")


def reset_app_state() -> None:
    """The lifespan reuses config/store from app.state when present (a test
    injection hook) — clear them so each test gets its own tmp paths."""
    for attr in ("config", "store"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """A clean environment for one app lifetime: auth on, admin/hunter2, no
    config file, an empty data directory. Returns that directory."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    monkeypatch.setenv("RESTRUO_USERNAME", "admin")
    reset_app_state()
    return tmp_path


@pytest.fixture
def client(app_env):
    with TestClient(app) as test_client:
        yield test_client
