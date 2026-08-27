"""Login must satisfy Portainer's CSRF layer.

Portainer 2.20.2+ validates Origin/Referer on state-changing requests —
/api/auth included. A login sent without them is refused with
"Forbidden - referer not supplied", which is what stranded an instance as
unreachable after its Portainer was recreated: the session was gone, so every
poll tried to log in, and every login was rejected.
"""
import httpx
import pytest

from app.instances import InstanceRecord
from app.portainer import PortainerClient, PortainerError

RECORD = InstanceRecord(
    id=1,
    name="DS",
    base_url="https://portainer.test:9443",
    auth_type="credentials",
    username="admin",
    password="secret",
)


def portainer_like(*, enforce_referer: bool = True):
    """A Portainer that refuses state-changing requests lacking a same-origin
    Referer, exactly as its csrf.go does."""
    seen = {"logins": 0, "login_referer": None}

    def handler(request: httpx.Request) -> httpx.Response:
        mutating = request.method not in ("GET", "HEAD", "OPTIONS")
        if mutating and enforce_referer and not request.headers.get("Referer"):
            return httpx.Response(403, text="Forbidden - referer not supplied")
        if request.url.path == "/api/auth":
            seen["logins"] += 1
            seen["login_referer"] = request.headers.get("Referer")
            return httpx.Response(200, json={"jwt": "jwt-token"},
                                  headers={"X-CSRF-Token": "csrf-token"})
        if request.url.path == "/api/endpoints":
            if request.headers.get("Authorization") != "Bearer jwt-token":
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json=[{"Id": 1, "Name": "local"}],
                                  headers={"X-CSRF-Token": "csrf-token"})
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_login_sends_same_origin_headers():
    transport, seen = portainer_like()
    client = PortainerClient(RECORD, transport=transport)

    endpoints = await client.list_endpoints()

    assert endpoints == [{"Id": 1, "Name": "local"}]
    assert seen["login_referer"] == "https://portainer.test:9443/"


@pytest.mark.asyncio
async def test_instance_recovers_after_portainer_is_recreated():
    """The regression: a recreated Portainer drops the session, so the client
    must be able to log in again unaided."""
    transport, seen = portainer_like()
    client = PortainerClient(RECORD, transport=transport)
    await client.list_endpoints()

    # Portainer is recreated: JWT and cookies are worthless.
    await client.reconnect()

    assert await client.list_endpoints() == [{"Id": 1, "Name": "local"}]
    assert seen["logins"] == 2


@pytest.mark.asyncio
async def test_login_failure_is_reported_not_swallowed():
    """If login is refused for some other reason, say so — a silent failure is
    what made this take three attempts to find."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Invalid credentials"})

    client = PortainerClient(RECORD, transport=httpx.MockTransport(handler))
    with pytest.raises(PortainerError):
        await client.list_endpoints()
