"""Integration tests for the session-cookie login flow."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _reset_app_state():
    # The lifespan reuses config/store from app.state when present (test
    # injection hook) — clear them so each test gets its own tmp paths.
    for attr in ("config", "store"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))  # defaults: auth on
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    monkeypatch.setenv("RESTRUO_USERNAME", "admin")
    _reset_app_state()
    with TestClient(app) as test_client:
        yield test_client


def test_shell_and_branding_are_public(client):
    assert client.get("/").status_code == 200
    assert client.get("/icon.svg").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/icons/icon-192.png").status_code == 200
    config = client.get("/api/ui-config").json()
    assert config["title"] == "Restruo"
    assert config["authEnabled"] is True


def test_data_endpoints_require_auth_without_basic_challenge(client):
    response = client.get("/api/instances")
    assert response.status_code == 401
    # No WWW-Authenticate header — the browser must show our form, not its dialog.
    assert "www-authenticate" not in response.headers


def test_login_sets_session_cookie_that_authenticates(client):
    bad = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401

    good = client.post("/api/login", json={"username": "admin", "password": "hunter2"})
    assert good.status_code == 200
    assert "restruo_session" in good.cookies

    # TestClient keeps the cookie jar — data endpoints now work.
    assert client.get("/api/instances").status_code == 200
    assert client.get("/api/updates").status_code == 200


def test_logout_clears_the_session(client):
    client.post("/api/login", json={"username": "admin", "password": "hunter2"})
    assert client.get("/api/instances").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/instances").status_code == 401


def test_basic_auth_still_works_for_scripts(client):
    response = client.get("/api/instances", auth=("admin", "hunter2"))
    assert response.status_code == 200


def test_tampered_session_cookie_is_rejected(client):
    client.cookies.set("restruo_session", "9999999999.deadbeef")
    assert client.get("/api/instances").status_code == 401


def test_session_secret_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    _reset_app_state()
    with TestClient(app):
        pass
    secret = tmp_path / "session_secret"
    assert (secret.stat().st_mode & 0o777) == 0o600


def test_sessions_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    _reset_app_state()
    with TestClient(app) as first:
        token = first.post(
            "/api/login", json={"username": "admin", "password": "hunter2"}
        ).cookies["restruo_session"]
    # New lifespan = container restart; the persisted secret still validates it.
    with TestClient(app) as second:
        second.cookies.set("restruo_session", token)
        assert second.get("/api/instances").status_code == 200
