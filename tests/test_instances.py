"""Tests for the instance store and username/password (JWT) authentication."""

import json

import httpx
import pytest

from app.instances import InstanceRecord, InstanceStore
from app.portainer import PortainerClient, PortainerError

CRED_INSTANCE = InstanceRecord(
    id=1, name="Cred", base_url="https://portainer.test:9443", verify_tls=True,
    auth_type="credentials", username="admin", password="hunter2",
)


# --- InstanceStore ----------------------------------------------------------

async def test_store_crud_and_persistence(tmp_path):
    path = tmp_path / "instances.json"
    store = InstanceStore(path)
    assert store.list() == []

    record = await store.add({
        "name": "A", "base_url": "https://a.test:9443/",
        "auth_type": "api_key", "api_key": "ptr_a",
    })
    assert record.id == 1
    assert record.base_url == "https://a.test:9443"  # trailing slash stripped

    # Reload from disk — persisted.
    reloaded = InstanceStore(path)
    assert reloaded.get(1).name == "A"

    # Update without secret keeps the stored secret.
    updated = await store.update(1, {
        "name": "A2", "base_url": "https://a.test:9443",
        "auth_type": "api_key", "api_key": "",
    })
    assert updated.name == "A2"
    assert updated.api_key == "ptr_a"

    # Switching auth type requires the new secret.
    switched = await store.update(1, {
        "name": "A2", "base_url": "https://a.test:9443",
        "auth_type": "credentials", "username": "admin", "password": "pw",
    })
    assert switched.auth_type == "credentials"

    assert await store.delete(1) is True
    assert await store.delete(1) is False
    assert store.list() == []


async def test_store_validation_rejects_missing_secrets(tmp_path):
    store = InstanceStore(tmp_path / "instances.json")
    with pytest.raises(ValueError):
        await store.add({"name": "X", "base_url": "https://x.test", "auth_type": "api_key"})
    with pytest.raises(ValueError):
        await store.add({
            "name": "X", "base_url": "https://x.test",
            "auth_type": "credentials", "username": "admin",
        })


async def test_store_seed(tmp_path):
    path = tmp_path / "instances.json"
    store = InstanceStore(path)
    await store.seed([
        {"name": "A", "base_url": "https://a.test", "auth_type": "api_key", "api_key": "k1"},
        {"name": "B", "base_url": "https://b.test", "auth_type": "api_key", "api_key": "k2"},
    ])
    assert [r.id for r in store.list()] == [1, 2]
    assert json.loads(path.read_text())[1]["name"] == "B"


def test_public_shape_has_no_secrets():
    public = CRED_INSTANCE.public()
    assert "api_key" not in str(public)
    assert "hunter2" not in str(public)
    assert public["authType"] == "credentials"
    assert public["username"] == "admin"


# --- credentials (JWT) auth flow -------------------------------------------

def jwt_portainer_transport(state: dict) -> httpx.MockTransport:
    """Mock Portainer requiring a JWT; `state` records logins and lets a test
    invalidate the current token to simulate expiry."""
    state.setdefault("logins", 0)
    state.setdefault("valid_jwt", None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            body = json.loads(request.content)
            if body != {"Username": "admin", "Password": "hunter2"}:
                return httpx.Response(422, json={"message": "invalid credentials"})
            state["logins"] += 1
            state["valid_jwt"] = f"jwt-{state['logins']}"
            return httpx.Response(200, json={"jwt": state["valid_jwt"]})
        if request.headers.get("Authorization") != f"Bearer {state['valid_jwt']}":
            return httpx.Response(401, json={"message": "unauthorized"})
        if request.url.path == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 2}])
        raise AssertionError(f"unexpected request: {request.url.path}")

    return httpx.MockTransport(handler)


async def test_credentials_login_and_reuse():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=jwt_portainer_transport(state))
    assert await client.list_endpoints() == [{"Id": 2}]
    assert await client.list_endpoints() == [{"Id": 2}]
    assert state["logins"] == 1  # session JWT reused, not re-fetched per call
    await client.aclose()


async def test_credentials_relogin_on_expiry():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=jwt_portainer_transport(state))
    await client.list_endpoints()
    state["valid_jwt"] = "expired"  # simulate Portainer expiring the session
    assert await client.list_endpoints() == [{"Id": 2}]
    assert state["logins"] == 2
    await client.aclose()


async def test_credentials_bad_password_surfaces_error():
    state = {}
    bad = CRED_INSTANCE.model_copy(update={"password": "wrong"})
    client = PortainerClient(bad, transport=jwt_portainer_transport(state))
    with pytest.raises(PortainerError) as excinfo:
        await client.list_endpoints()
    assert "invalid credentials" in excinfo.value.message
    await client.aclose()


# --- CSRF handling (Portainer 2.20.2+ session-auth requirement) --------------

GIT_STACK = {
    "Id": 5, "Name": "s", "EndpointId": 2, "Type": 2,
    "GitConfig": {"URL": "https://github.com/x/y"}, "Env": [],
}


def csrf_portainer_transport(state: dict) -> httpx.MockTransport:
    """Mock Portainer that enforces X-CSRF-Token on mutating JWT requests.
    The current token is issued via a response header on GET /."""
    state.setdefault("csrf_fetches", 0)
    state.setdefault("valid_csrf", "csrf-1")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth":
            return httpx.Response(200, json={"jwt": "jwt-1"})
        if path == "/" and request.method == "GET":
            # The SPA page is public and issues the CSRF token via header.
            state["csrf_fetches"] += 1
            return httpx.Response(200, headers={"X-CSRF-Token": state["valid_csrf"]}, text="<html>")
        if request.headers.get("Authorization") != "Bearer jwt-1":
            return httpx.Response(401, json={"message": "unauthorized"})
        if request.method in ("PUT", "POST", "DELETE"):
            if request.headers.get("X-CSRF-Token") != state["valid_csrf"]:
                return httpx.Response(403, text="Forbidden - CSRF token not found in request")
        if path == "/api/stacks/5/git/redeploy":
            return httpx.Response(200, json={"Id": 5})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return httpx.MockTransport(handler)


async def test_credentials_mutating_request_sends_csrf_token():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=csrf_portainer_transport(state))
    await client.update_stack(GIT_STACK)
    assert state["csrf_fetches"] == 1
    # Token is reused on the next mutating call, not re-fetched.
    await client.update_stack(GIT_STACK)
    assert state["csrf_fetches"] == 1
    await client.aclose()


async def test_credentials_refreshes_stale_csrf_token():
    state = {}
    client = PortainerClient(CRED_INSTANCE, transport=csrf_portainer_transport(state))
    await client.update_stack(GIT_STACK)
    state["valid_csrf"] = "csrf-2"  # server rotated the token
    await client.update_stack(GIT_STACK)  # 403 → re-fetch → retry succeeds
    assert state["csrf_fetches"] == 2
    await client.aclose()
