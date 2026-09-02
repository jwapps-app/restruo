"""Restruo — multi-instance Portainer stack updater dashboard."""

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict

from .auth import SESSION_COOKIE, SESSION_TTL_SECONDS, LoginLimiter, SessionManager
from .config import AppConfig, load_config
from .instances import ClientManager, InstanceRecord, InstanceStore
from .notifiers import EmailNotifier, UpdateEvent, build_notifiers, compose_body
from .portainer import (
    PortainerClient,
    PortainerError,
    container_is_down,
    container_name,
    cannot_recreate_image,
    is_self_critical_image,
    normalize_container,
    normalize_stack,
    resolve_image_name,
    stack_containers,
    stack_images,
    standalone_containers,
)
from .registry import RegistryClient
from .updates import UpdateChecker

logger = logging.getLogger("restruo")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: AppConfig = getattr(app.state, "config", None) or load_config()
    app.state.config = config

    store: InstanceStore = getattr(app.state, "store", None) or InstanceStore()
    app.state.store = store
    # Redeploys in progress, so a second request for the same one is refused
    # with a reason instead of colliding inside Portainer.
    app.state.in_flight: set = set()
    if not store.exists and config.instances:
        # One-time import of instances defined in config.yaml.
        await store.seed(
            [
                {
                    "name": i.name,
                    "base_url": i.base_url,
                    "verify_tls": i.verify_tls,
                    "auth_type": "api_key",
                    "api_key": i.api_key,
                }
                for i in config.instances
            ]
        )
        logger.info("Imported %d instance(s) from config.yaml", len(config.instances))

    manager = ClientManager(store)
    await manager.refresh()
    app.state.manager = manager
    app.state.sessions = SessionManager(
        store.path.parent / "session_secret", config.ui.auth.password
    )
    app.state.limiter = LoginLimiter()
    # Redeploys that outlived the request that started them; see _run_job.
    app.state.jobs: dict[str, dict] = {}

    app.state.registry = RegistryClient(credentials={
        host: (creds.split(":", 1)[0], creds.split(":", 1)[1])
        for host, creds in config.updates.registry_auth.items()
    })
    app.state.checker = UpdateChecker(
        manager.items,
        app.state.registry,
        interval_hours=config.updates.interval_hours,
        notifiers=build_notifiers(config),
        floating_tags=config.updates.floating_tags,
        state_path=store.path.parent / "notified.json",
    )
    checker_task = None
    if config.updates.enabled:
        checker_task = asyncio.create_task(app.state.checker.run_periodic())
    logger.info("Managing %d Portainer instance(s)", len(store.list()))
    yield
    if checker_task:
        checker_task.cancel()
    await manager.aclose()
    await app.state.registry.aclose()


app = FastAPI(title="Restruo", lifespan=lifespan)


@app.middleware("http")
async def response_headers(request: Request, call_next):
    """Live state must never be served from a browser cache — a stale
    'unreachable' would outlive the outage that caused it. And the dashboard
    must not be framed: "same-site" ignores the port, so any other web UI on
    the same host could otherwise embed it with the session cookie attached."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response

_basic = HTTPBasic(auto_error=False)


def _credentials_valid(request: Request, username: str, password: str) -> bool:
    auth = request.app.state.config.ui.auth
    return (
        secrets.compare_digest(username.encode(), auth.username.encode())
        and secrets.compare_digest(password.encode(), (auth.password or "").encode())
    )


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# A cookie-authenticated request that changes something must carry this
# header. A form or a no-cors fetch from another page cannot add a custom
# header, so it cannot ride the session cookie — which SameSite=Lax alone does
# not prevent from a page on another port of the same host. Basic-auth callers
# (curl, scripts) never carry the cookie and are unaffected.
CSRF_HEADER = "X-Restruo"


def _client_addr(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "").lower() == "https"


def _session_valid(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token) and request.app.state.sessions.verify(token)


def _require_csrf_header(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(
            status_code=403,
            detail=f"Missing {CSRF_HEADER} header — a browser session must send it "
                   "on every request that changes something.",
        )


def _basic_auth_ok(request: Request, credentials: HTTPBasicCredentials | None) -> bool:
    if credentials is None:
        return False
    limiter: LoginLimiter = request.app.state.limiter
    addr = _client_addr(request)
    if limiter.blocked(addr):
        raise HTTPException(
            status_code=429, detail="Too many failed logins — try again later."
        )
    if _credentials_valid(request, credentials.username, credentials.password):
        limiter.reset(addr)
        return True
    limiter.record_failure(addr)
    logger.warning("Failed login for %r from %s", credentials.username, addr)
    return False


def require_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)):
    auth = request.app.state.config.ui.auth
    if not auth.enabled:
        return
    if _session_valid(request):
        _require_csrf_header(request)
        return
    if _basic_auth_ok(request, credentials):
        return
    # No WWW-Authenticate header: the app has its own login form, and the
    # header would make browsers pop the (slow) native basic-auth dialog.
    raise HTTPException(status_code=401, detail="Unauthorized")


def _authenticated(request: Request, credentials: HTTPBasicCredentials | None) -> bool:
    """Like require_auth, but a question rather than a gate."""
    if not request.app.state.config.ui.auth.enabled:
        return True
    if _session_valid(request):
        return True
    try:
        return _basic_auth_ok(request, credentials)
    except HTTPException:
        return False


class LoginRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    username: str
    password: str


@app.post("/api/login")
async def login(request: Request, body: LoginRequest):
    auth = request.app.state.config.ui.auth
    if auth.enabled:
        limiter: LoginLimiter = request.app.state.limiter
        addr = _client_addr(request)
        if limiter.blocked(addr):
            raise HTTPException(
                status_code=429, detail="Too many failed logins — try again later."
            )
        if not _credentials_valid(request, body.username, body.password):
            limiter.record_failure(addr)
            logger.warning("Failed login for %r from %s", body.username, addr)
            await asyncio.sleep(1)
            raise HTTPException(status_code=401, detail="Wrong username or password.")
        limiter.reset(addr)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.sessions.issue(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )
    return response


@app.post("/api/logout")
async def logout(request: Request):
    if _session_valid(request):
        _require_csrf_header(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


def _manager(request: Request) -> ClientManager:
    return request.app.state.manager


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# --- instance management ----------------------------------------------------


class InstanceInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    name: str
    baseUrl: str
    verifyTls: bool = True
    authType: str = "api_key"
    apiKey: str | None = None
    username: str | None = None
    password: str | None = None

    def to_fields(self) -> dict:
        return {
            "name": self.name,
            "base_url": self.baseUrl,
            "verify_tls": self.verifyTls,
            "auth_type": self.authType,
            "api_key": self.apiKey,
            "username": self.username,
            "password": self.password,
        }


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).netloc or url).lower()


async def _probe_record(record: InstanceRecord) -> dict:
    """Try listing endpoints with the record's credentials."""
    client = PortainerClient(record)
    try:
        endpoints = await client.list_endpoints()
        return {"ok": True, "error": None, "endpoints": len(endpoints)}
    except PortainerError as exc:
        return {"ok": False, "error": exc.message, "endpoints": 0}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "endpoints": 0}
    finally:
        await client.aclose()


@app.get("/api/instances", dependencies=[Depends(require_auth)])
async def list_instances(request: Request):
    async def probe(iid: int, client: PortainerClient) -> dict:
        record = request.app.state.store.get(iid)
        entry = {**record.public(), "reachable": True, "error": None}
        try:
            await client.list_endpoints()
        except PortainerError as exc:
            entry.update(reachable=False, error=exc.message)
            logger.warning("Instance %r unreachable: %s", record.name, exc.message)
            await client.reconnect()
        except Exception as exc:
            entry.update(reachable=False, error=str(exc))
            logger.warning(
                "Instance %r unreachable: %s: %s", record.name, type(exc).__name__, exc
            )
            await client.reconnect()
        return entry

    return await asyncio.gather(
        *(probe(iid, client) for iid, client in _manager(request).items())
    )


@app.post("/api/instances", dependencies=[Depends(require_auth)])
async def add_instance(request: Request, body: InstanceInput):
    store: InstanceStore = request.app.state.store
    try:
        record = await store.add(body.to_fields())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await _manager(request).refresh()
    return record.public()


@app.put("/api/instances/{iid}", dependencies=[Depends(require_auth)])
async def edit_instance(request: Request, iid: int, body: InstanceInput):
    store: InstanceStore = request.app.state.store
    try:
        record = await store.update(iid, body.to_fields())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if record is None:
        raise HTTPException(status_code=404, detail=f"No instance with id {iid}")
    await _manager(request).refresh()
    return record.public()


class MoveInput(BaseModel):
    direction: str


@app.post("/api/instances/{iid}/move", dependencies=[Depends(require_auth)])
async def move_instance(request: Request, iid: int, body: MoveInput):
    """Reorder an instance. Stored order drives the dashboard and this list."""
    if body.direction not in ("up", "down"):
        raise HTTPException(status_code=422, detail="direction must be 'up' or 'down'")
    store: InstanceStore = request.app.state.store
    if store.get(iid) is None:
        raise HTTPException(status_code=404, detail=f"No instance with id {iid}")
    # No client rebuild needed: the manager reads store order on every call, so
    # sessions and CSRF tokens survive a reorder.
    await store.move(iid, body.direction)
    return [r.public() for r in store.list()]


@app.delete("/api/instances/{iid}", dependencies=[Depends(require_auth)])
async def delete_instance(request: Request, iid: int):
    if not await request.app.state.store.delete(iid):
        raise HTTPException(status_code=404, detail=f"No instance with id {iid}")
    await _manager(request).refresh()
    return {"ok": True}


@app.post("/api/instances/test", dependencies=[Depends(require_auth)])
async def test_instance(request: Request, body: InstanceInput, id: int | None = None):
    """Test a connection with form values. When editing (id given) and the
    secret field was left blank, the stored secret is used."""
    fields = body.to_fields()
    if id is not None:
        existing = request.app.state.store.get(id)
        if existing:
            reusing = (fields["auth_type"] == "api_key" and not fields["api_key"]) or \
                (fields["auth_type"] == "credentials" and not fields["password"])
            if reusing and _host_of(fields["base_url"]) != _host_of(existing.base_url):
                # A stored secret is only ever sent to the address it was
                # saved for. Anything else would let a request that names a
                # new URL collect the credential from the server.
                return {"ok": False, "endpoints": 0,
                        "error": "The address changed — enter the API key or "
                                 "password again to test it there."}
            if fields["auth_type"] == "api_key" and not fields["api_key"]:
                fields["api_key"] = existing.api_key
            if fields["auth_type"] == "credentials" and not fields["password"]:
                fields["password"] = existing.password
    try:
        record = InstanceRecord.model_validate({**fields, "id": 0})
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "endpoints": 0}
    return await _probe_record(record)


# --- stacks -------------------------------------------------------------------


async def _stacks_for_instance(iid: int, name: str, client: PortainerClient) -> dict:
    result = {
        "instance": {"id": iid, "name": name},
        "stacks": [],
        "containers": [],
        "reachable": True,
        "error": None,
    }
    try:
        stacks = await client.list_stacks()
    except PortainerError as exc:
        result.update(reachable=False, error=exc.message)
        logger.warning("Instance %r unreachable: %s", name, exc.message)
        # Whatever went wrong, start the next poll from a clean connection and
        # a fresh login — the same reset that re-saving the instance performs.
        await client.reconnect()
        return result
    except Exception as exc:
        result.update(reachable=False, error=str(exc))
        logger.warning(
            "Instance %r unreachable: %s: %s", name, type(exc).__name__, exc
        )
        await client.reconnect()
        return result

    async def images_for(stack: dict, own: list[dict]) -> list[str]:
        try:
            content = await client.get_stack_file(stack["Id"])
        except Exception:
            return []
        return stack_images(stack, content, own)

    containers_by_endpoint: dict[int, list[dict]] = {}
    # One Portainer can manage several environments (agents, remote hosts) —
    # keep their names so each row can say where it actually runs.
    environments: dict[int, str] = {}
    try:
        endpoints = await client.list_endpoints()
    except Exception:
        endpoints = []
    for endpoint in endpoints:
        environments[endpoint["Id"]] = endpoint.get("Name") or f"env {endpoint['Id']}"

    async def containers_of(endpoint_id: int) -> tuple[int, list[dict] | None]:
        try:
            return endpoint_id, await client.list_containers(endpoint_id)
        except Exception:
            return endpoint_id, None

    # Environments are independent hosts; asking them one after another
    # makes the page wait on the slowest agent times the number of agents.
    for endpoint_id, containers in await asyncio.gather(
        *(containers_of(e["Id"]) for e in endpoints)
    ):
        if containers is not None:
            containers_by_endpoint[endpoint_id] = containers
    result["environments"] = len(environments)

    owned = [
        stack_containers(stack, containers_by_endpoint.get(stack.get("EndpointId"), []))
        for stack in stacks
    ]
    image_lists = await asyncio.gather(
        *(images_for(stack, own) for stack, own in zip(stacks, owned))
    )
    for stack, images, own in zip(stacks, image_lists, owned):
        normalized = normalize_stack(stack, images)
        normalized["containersTotal"] = len(own)
        normalized["downNames"] = [container_name(c) for c in own if container_is_down(c)]
        normalized["environment"] = environments.get(stack.get("EndpointId"), "")
        # Stacks running Portainer or Restruo can't be stopped from here.
        normalized["selfCritical"] = any(
            is_self_critical_image(c.get("Image", "")) for c in own
        ) or any(is_self_critical_image(i) for i in images)
        normalized["updateProtected"] = any(
            cannot_recreate_image(c.get("Image", "")) for c in own
        ) or any(cannot_recreate_image(i) for i in images)
        result["stacks"].append(normalized)

    # Containers that live outside any Portainer stack.
    stack_names = {s.get("Name") for s in stacks}

    async def standalone_row(endpoint_id: int, c: dict) -> dict:
        normalized = normalize_container(c, endpoint_id)
        normalized["image"] = await resolve_image_name(client, endpoint_id, c)
        normalized["environment"] = environments.get(endpoint_id, "")
        return normalized

    result["containers"] = list(await asyncio.gather(*(
        standalone_row(endpoint_id, c)
        for endpoint_id, containers in containers_by_endpoint.items()
        for c in standalone_containers(containers, stack_names)
    )))
    return result


@app.get("/api/stacks", dependencies=[Depends(require_auth)])
async def list_all_stacks(request: Request):
    return await asyncio.gather(
        *(
            _stacks_for_instance(iid, client.instance.name, client)
            for iid, client in _manager(request).items()
        )
    )


def _get_client(request: Request, iid: int) -> PortainerClient:
    client = _manager(request).get(iid)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No instance with id {iid}")
    return client


@asynccontextmanager
async def _exclusive(request: Request, key: tuple):
    """One redeploy at a time per stack or container.

    Portainer refuses a second deploy of the same stack while one is running,
    and answers with a generic "Unable to update stack" — which reads as a
    failure of the update rather than of the timing. A redeploy can take a
    minute while compose waits on a healthcheck, which is ample time to click
    again, or to click from another tab.
    """
    in_flight = request.app.state.in_flight
    if key in in_flight:
        raise HTTPException(
            status_code=409,
            detail="An update is already running for this one — it takes a "
                   "moment while Portainer waits for the containers to come up.",
        )
    in_flight.add(key)
    try:
        yield
    finally:
        in_flight.discard(key)


# Portainer accepts a stack deploy and returns immediately, running compose in
# the background — a redeploy the API "completed" in 68ms can still be pulling
# an image a minute later. So watch the stack's containers until they settle,
# and report what actually happened rather than what was accepted.
DEPLOY_POLL_SECONDS = 2.0
DEPLOY_SETTLE_POLLS = 2      # unchanged this many times running = finished
DEPLOY_TIMEOUT_SECONDS = 240.0


async def _stack_fingerprint(client: PortainerClient, stack: dict) -> frozenset | None:
    """Which containers the stack is running. A compose recreate replaces them,
    so a change here is the deploy actually landing. None when the listing
    itself failed — which says nothing about the stack, and must not be read
    as "every container vanished" and then "redeployed"."""
    try:
        own = stack_containers(
            stack, await client.list_containers(stack["EndpointId"])
        )
    except Exception:
        return None
    return frozenset(
        (c.get("Id"), c.get("State"), c.get("Status")) for c in own
    )


async def _await_deploy(client: PortainerClient, stack: dict, before: frozenset) -> str:
    """Block until the deploy settles. Returns a description of the outcome."""
    deadline = time.monotonic() + DEPLOY_TIMEOUT_SECONDS
    changed_at: float | None = None
    last = before
    stable = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(DEPLOY_POLL_SECONDS)
        current = await _stack_fingerprint(client, stack)
        if current is None:
            continue
        if current != last:
            changed_at = time.monotonic()
            last = current
            stable = 0
            continue
        if changed_at is not None:
            stable += 1
            if stable >= DEPLOY_SETTLE_POLLS:
                recreated = len({c[0] for c in last} - {c[0] for c in before})
                if recreated:
                    return f"Redeployed {recreated} container{'' if recreated == 1 else 's'}."
                return "Redeployed."
    if changed_at is None:
        return ("Portainer accepted the redeploy and nothing changed — the images "
                "were already current, so no container was recreated.")
    return ("Still deploying after "
            f"{int(DEPLOY_TIMEOUT_SECONDS)}s — Portainer is finishing in the background.")


# A redeploy can run for minutes. Answer inline when it finishes quickly, and
# otherwise hand the page a job id to poll — an HTTP request that stays open
# for four minutes is fine on a LAN and dead on arrival behind most proxies,
# which cut it off around 100 s while the deploy carries on regardless.
JOB_SYNC_WAIT_SECONDS = 25.0
JOB_RETENTION_SECONDS = 3600.0


def _prune_jobs(jobs: dict[str, dict]) -> None:
    cutoff = time.time() - JOB_RETENTION_SECONDS
    for job_id in [j for j, job in jobs.items() if job["done"] and job["started"] < cutoff]:
        del jobs[job_id]


async def _run_job(request: Request, key: tuple, work) -> JSONResponse:
    """Run `work()` under the per-target lock and report its result."""
    app = request.app
    if key in app.state.in_flight:
        raise HTTPException(
            status_code=409,
            detail="An update is already running for this one — it takes a "
                   "moment while Portainer waits for the containers to come up.",
        )
    job = {"id": secrets.token_hex(8), "done": False, "result": None,
           "started": time.time()}
    _prune_jobs(app.state.jobs)
    app.state.jobs[job["id"]] = job

    async def runner() -> None:
        try:
            async with _exclusive(request, key):
                job["result"] = await work()
        except HTTPException as exc:
            job["result"] = {"ok": False, "message": exc.detail}
        except Exception as exc:
            job["result"] = {"ok": False, "message": describe(exc)}
        finally:
            job["done"] = True

    task = asyncio.create_task(runner())
    try:
        await asyncio.wait_for(asyncio.shield(task), JOB_SYNC_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=202, content={
            "ok": None, "jobId": job["id"],
            "message": "Still deploying — Restruo keeps watching it.",
        })
    result = job["result"] or {"ok": False, "message": "No result recorded."}
    return JSONResponse(status_code=200 if result.get("ok") else 502, content=result)


def describe(exc: Exception) -> str:
    return exc.message if isinstance(exc, PortainerError) else (str(exc) or type(exc).__name__)


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(request: Request, job_id: str):
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job — it may have expired.")
    return {"done": job["done"], "result": job["result"]}


async def _update_one(client: PortainerClient, stack: dict) -> dict:
    name = stack.get("Name", f"stack {stack.get('Id')}")
    started = time.monotonic()
    before = await _stack_fingerprint(client, stack) or frozenset()
    try:
        await client.update_stack(stack)
    except PortainerError as exc:
        return {
            "ok": False,
            "stack": name,
            "durationMs": int((time.monotonic() - started) * 1000),
            "message": exc.message,
        }
    except Exception as exc:
        return {
            "ok": False,
            "stack": name,
            "durationMs": int((time.monotonic() - started) * 1000),
            "message": str(exc),
        }
    message = await _await_deploy(client, stack, before)
    return {
        "ok": True,
        "stack": name,
        "durationMs": int((time.monotonic() - started) * 1000),
        "message": message,
    }


async def _find_container(
    client: PortainerClient, cid: str, endpoint_id: int | None = None
) -> tuple[int, dict]:
    """Locate a container, preferring the environment the caller names.

    Container ids are unique per host, not per Portainer: machines cloned from
    a template carry the same ids. Scanning environments in order would then
    act on whichever host happens to come first — a different machine than the
    row that was clicked.
    """
    if endpoint_id is not None:
        for container in await client.list_containers(endpoint_id):
            if container.get("Id") == cid:
                return endpoint_id, container
        raise HTTPException(
            status_code=404,
            detail=f"No container {cid[:12]} in environment {endpoint_id}",
        )
    for endpoint in await client.list_endpoints():
        for container in await client.list_containers(endpoint["Id"]):
            if container.get("Id") == cid:
                return endpoint["Id"], container
    raise HTTPException(status_code=404, detail=f"No container {cid[:12]} on this instance")


def _timed(started: float, name: str, message: str, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "stack": name,
        "durationMs": int((time.monotonic() - started) * 1000),
        "message": message,
    }


async def _set_stack_state(request: Request, iid: int, sid: int, action: str):
    """Start or stop a stack."""
    client = _get_client(request, iid)
    started = time.monotonic()
    try:
        stack = await client.get_stack(sid)
    except PortainerError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=f"No stack with id {sid} on this instance")
        raise HTTPException(status_code=502, detail=f"Could not fetch stack: {exc.message}")

    name = stack.get("Name", f"stack {sid}")
    if action == "stop":
        try:
            containers = stack_containers(
                stack, await client.list_containers(stack["EndpointId"])
            )
        except Exception:
            containers = []
        if any(is_self_critical_image(c.get("Image", "")) for c in containers):
            raise HTTPException(
                status_code=400,
                detail=f"“{name}” runs Portainer or Restruo itself — stopping it from here "
                       "would cut off the connection needed to start it again.",
            )
    try:
        await client.set_stack_state(sid, stack["EndpointId"], running=action == "start")
    except Exception as exc:
        message = exc.message if isinstance(exc, PortainerError) else str(exc)
        return JSONResponse(status_code=502, content=_timed(started, name, message, ok=False))
    return _timed(started, name, "Started." if action == "start" else "Stopped.")


@app.post("/api/instances/{iid}/stacks/{sid}/start", dependencies=[Depends(require_auth)])
async def start_stack(request: Request, iid: int, sid: int):
    return await _set_stack_state(request, iid, sid, "start")


@app.post("/api/instances/{iid}/stacks/{sid}/stop", dependencies=[Depends(require_auth)])
async def stop_stack(request: Request, iid: int, sid: int):
    return await _set_stack_state(request, iid, sid, "stop")


async def _set_container_state(
    request: Request, iid: int, cid: str, action: str, endpoint_id: int | None = None
):
    """Start or stop a standalone container."""
    client = _get_client(request, iid)
    started = time.monotonic()
    try:
        endpoint_id, container = await _find_container(client, cid, endpoint_id)
    except HTTPException:
        raise
    except Exception as exc:
        message = exc.message if isinstance(exc, PortainerError) else str(exc)
        raise HTTPException(status_code=502, detail=f"Could not find container: {message}")

    name = container_name(container)
    image = await resolve_image_name(client, endpoint_id, container)
    if action == "stop" and is_self_critical_image(image):
        raise HTTPException(
            status_code=400,
            detail=f"“{name}” is Portainer or Restruo itself — stopping it from here would "
                   "cut off the connection needed to start it again.",
        )
    try:
        await client.set_container_state(endpoint_id, cid, running=action == "start")
    except Exception as exc:
        message = exc.message if isinstance(exc, PortainerError) else str(exc)
        return JSONResponse(status_code=502, content=_timed(started, name, message, ok=False))
    return _timed(started, name, "Started." if action == "start" else "Stopped.")


@app.post("/api/instances/{iid}/containers/{cid}/start", dependencies=[Depends(require_auth)])
async def start_container(
    request: Request, iid: int, cid: str, endpointId: int | None = None
):
    return await _set_container_state(request, iid, cid, "start", endpointId)


@app.post("/api/instances/{iid}/containers/{cid}/stop", dependencies=[Depends(require_auth)])
async def stop_container(
    request: Request, iid: int, cid: str, endpointId: int | None = None
):
    return await _set_container_state(request, iid, cid, "stop", endpointId)


@app.post("/api/instances/{iid}/stacks/{sid}/update", dependencies=[Depends(require_auth)])
async def update_stack(request: Request, iid: int, sid: int):
    client = _get_client(request, iid)
    # Re-fetch the stack so Env / EndpointId are current at redeploy time.
    try:
        stack = await client.get_stack(sid)
    except PortainerError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=f"No stack with id {sid} on this instance")
        raise HTTPException(status_code=502, detail=f"Could not fetch stack: {exc.message}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch stack: {exc}")

    try:
        own = stack_containers(stack, await client.list_containers(stack["EndpointId"]))
    except Exception:
        own = []
    blocked = next(
        (c for c in own if cannot_recreate_image(c.get("Image", ""))), None
    )
    if blocked is not None:
        raise HTTPException(
            status_code=400,
            detail=f"“{stack.get('Name', sid)}” runs Portainer or a Portainer agent. "
                   "Redeploying it would stop the container carrying the command, so "
                   "the redeploy could never finish. Update it from that host instead.",
        )

    async def work() -> dict:
        result = await _update_one(client, stack)
        if result["ok"]:
            request.app.state.checker.mark_updated(iid, stack_id=sid)
        return result

    return await _run_job(request, ("stack", iid, sid), work)


@app.post("/api/instances/{iid}/containers/{cid}/update", dependencies=[Depends(require_auth)])
async def update_container(
    request: Request, iid: int, cid: str, endpointId: int | None = None
):
    """Repull + recreate a standalone container via Portainer's recreate action."""
    client = _get_client(request, iid)
    started = time.monotonic()
    try:
        endpoint_id, container = await _find_container(client, cid, endpointId)
        resolved_image = await resolve_image_name(client, endpoint_id, container)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not find container: {describe(exc)}")

    if cannot_recreate_image(resolved_image):
        # Portainer relays this command through the very container being
        # replaced, so it stops and the recreate never completes — leaving the
        # environment offline with the new image pulled and unused.
        what = ("Portainer" if "portainer/portainer" in resolved_image.lower()
                else "the Portainer agent")
        raise HTTPException(
            status_code=400,
            detail=f"{what} can't be recreated through Portainer's own API — the "
                   "command travels through the container being replaced. Update "
                   "it from that host instead.",
        )

    name = container_name(container)

    async def work() -> dict:
        try:
            await client.recreate_container(endpoint_id, cid)
        except Exception as exc:
            return {"ok": False, "stack": name,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "message": describe(exc)}
        request.app.state.checker.mark_updated(
            iid, container_id=cid, endpoint_id=endpoint_id
        )
        return {"ok": True, "stack": name,
                "durationMs": int((time.monotonic() - started) * 1000),
                "message": "Repulled and recreated."}

    return await _run_job(request, ("container", iid, endpoint_id, cid), work)


class PruneRequest(BaseModel):
    images: bool = True
    # Off by default: it deletes the images of stopped stacks too.
    allImages: bool = False
    networks: bool = True
    volumes: bool = False


@app.post("/api/instances/{iid}/prune", dependencies=[Depends(require_auth)])
async def prune_instance(request: Request, iid: int, body: PruneRequest):
    """Remove unused Docker leftovers on every environment of one instance."""
    client = _get_client(request, iid)
    summary = {
        "ok": True, "spaceReclaimed": 0,
        "images": 0, "networks": 0, "volumes": 0, "errors": [],
    }
    try:
        endpoints = await client.list_endpoints()
    except Exception as exc:
        message = exc.message if isinstance(exc, PortainerError) else str(exc)
        raise HTTPException(status_code=502, detail=f"Could not list environments: {message}")

    def _msg(exc: Exception) -> str:
        return exc.message if isinstance(exc, PortainerError) else str(exc)

    for endpoint in endpoints:
        endpoint_id = endpoint["Id"]
        if body.images:
            try:
                pruned = await client.prune_images(endpoint_id, all_unused=body.allImages)
                summary["images"] += len(pruned.get("ImagesDeleted") or [])
                summary["spaceReclaimed"] += pruned.get("SpaceReclaimed") or 0
            except Exception as exc:
                summary["errors"].append(f"images: {_msg(exc)}")
        if body.networks:
            try:
                pruned = await client.prune_networks(endpoint_id)
                summary["networks"] += len(pruned.get("NetworksDeleted") or [])
            except Exception as exc:
                summary["errors"].append(f"networks: {_msg(exc)}")
        if body.volumes:
            try:
                pruned = await client.prune_volumes(endpoint_id)
                summary["volumes"] += len(pruned.get("VolumesDeleted") or [])
                summary["spaceReclaimed"] += pruned.get("SpaceReclaimed") or 0
            except Exception as exc:
                summary["errors"].append(f"volumes: {_msg(exc)}")
    summary["ok"] = not summary["errors"]
    return summary


# --- updates & UI -------------------------------------------------------------


@app.get("/api/updates", dependencies=[Depends(require_auth)])
async def get_updates(request: Request):
    return request.app.state.checker.snapshot()


@app.post("/api/check-updates", dependencies=[Depends(require_auth)])
async def check_updates(request: Request):
    return await request.app.state.checker.check_all()


# Title/version are cosmetic and shown on the login screen — no auth.
@app.post("/api/test-email", dependencies=[Depends(require_auth)])
async def test_email(request: Request):
    """Send a sample notification so SMTP settings can be proven now rather
    than the next time an update happens to appear."""
    email = request.app.state.config.email
    if not email.configured:
        raise HTTPException(
            status_code=400,
            detail="Email isn't configured — set RESTRUO_SMTP_HOST, RESTRUO_EMAIL_TO "
                   "and a sender (RESTRUO_EMAIL_FROM or RESTRUO_SMTP_USER).",
        )
    sample = [UpdateEvent("Example NAS", "jellyfin", "jellyfin/jellyfin:latest")]
    try:
        await EmailNotifier(email).deliver(
            "Restruo: test notification",
            "This is what an update notification looks like:\n\n"
            + compose_body(sample),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    return {"ok": True, "sentTo": email.recipients}


@app.get("/api/ui-config")
async def ui_config(
    request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)
):
    """What the login screen needs is public; who gets emailed is not."""
    config = request.app.state.config
    out = {
        "title": config.ui.title,
        "version": os.environ.get("RESTRUO_VERSION", "dev"),
        "authEnabled": config.ui.auth.enabled,
    }
    if _authenticated(request, credentials):
        out["refreshSeconds"] = config.ui.refresh_seconds
        out["email"] = {
            "configured": config.email.configured,
            "recipients": config.email.recipients,
            "host": config.email.host,
        }
    return out


@app.get("/icon.svg")
async def icon():
    return FileResponse(WEB_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/icons/{filename}")
async def icons(filename: str):
    path = (WEB_DIR / "icons" / filename).resolve()
    if path.parent != (WEB_DIR / "icons").resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)


# The app shell is public (it contains no data); every data endpoint stays
# behind auth. This makes first paint instant and lets the login form render
# immediately instead of blocking on the browser's basic-auth dialog.
@app.get("/")
async def index():
    # no-cache = revalidate on every load, so the UI can't go stale after an update.
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})
