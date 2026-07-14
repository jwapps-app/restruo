"""Integration tests for the session-cookie login flow."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))  # defaults: auth on
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    monkeypatch.setenv("RESTRUO_USERNAME", "admin")
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


def test_basic_auth_still_works_for_scripts(client):
    response = client.get("/api/instances", auth=("admin", "hunter2"))
    assert response.status_code == 200


def test_tampered_session_cookie_is_rejected(client):
    client.cookies.set("restruo_session", "9999999999.deadbeef")
    assert client.get("/api/instances").status_code == 401


def test_sessions_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "instances.json"))
    with TestClient(app) as first:
        token = first.post(
            "/api/login", json={"username": "admin", "password": "hunter2"}
        ).cookies["restruo_session"]
    # New lifespan = container restart; the persisted secret still validates it.
    with TestClient(app) as second:
        second.cookies.set("restruo_session", token)
        assert second.get("/api/instances").status_code == 200
