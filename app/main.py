"""Restruo — multi-instance Portainer stack updater dashboard."""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict

from .auth import SESSION_COOKIE, SESSION_TTL_SECONDS, LoginLimiter, SessionManager
from .config import AppConfig, load_config
from .instances import ClientManager, InstanceRecord, InstanceStore, destination
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
    stack_names_by_endpoint,
    standalone_containers,
)
from .registry import RegistryClient
from .updates import UpdateChecker

logger = logging.getLogger("restruo")


def _configure_logging() -> None:
    """Uvicorn sets up its own loggers and nothing else, so the app's INFO
    lines — the operation log, "Emailed N updates", the instance import —
    went nowhere. Give the app's logger a handler of its own, once."""
    root = logging.getLogger("restruo")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(os.environ.get("RESTRUO_LOG_LEVEL", "INFO").upper())
    root.propagate = False


_configure_logging()

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
        try:
            await checker_task
        except (asyncio.CancelledError, Exception):
            pass
    await manager.aclose()
    await app.state.registry.aclose()


# No generated API docs: the API is documented in the README, and the
# OpenAPI routes would be three more public pages describing every endpoint.
app = FastAPI(title="Restruo", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def validation_error_without_the_input(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 quotes the offending input back — which, for a
    login or an instance form, is a password or an API key. Say what was
    wrong with it, never what it was."""
    errors = [
        {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


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


async def _probe_record(record: InstanceRecord) -> dict:
    """Try listing endpoints with the record's credentials."""
    client = PortainerClient(record)
    try:
        endpoints = await client.list_endpoints()
        return {"ok": True, "error": None, "endpoints": len(endpoints)}
    except PortainerError as exc:
        return {"ok": False, "error": exc.message, "endpoints": 0}
    except Exception as exc:
        return {"ok": False, "error": describe(exc), "endpoints": 0}
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
            entry.update(reachable=False, error=describe(exc))
            logger.warning(
                "Instance %r unreachable: %s: %s", record.name, type(exc).__name__, exc
            )
            await client.reconnect()
        return entry

    return await asyncio.gather(
        *(probe(iid, client) for iid, client in _manager(request).items())
    )


def _audit(request: Request, what: str, *args) -> None:
    """One line per change made through the API, with where it came from.
    Failed logins were already logged; the things a login lets you do were not."""
    logger.info("[%s] " + what, _client_addr(request), *args)


@app.post("/api/instances", dependencies=[Depends(require_auth)])
async def add_instance(request: Request, body: InstanceInput):
    store: InstanceStore = request.app.state.store
    try:
        record = await store.add(body.to_fields())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _audit(request, "added instance %r (%s)", record.name, destination(record.base_url))
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
    _audit(request, "edited instance %r (%s)", record.name, destination(record.base_url))
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
    _audit(request, "deleted instance %d", iid)
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
            if reusing and destination(fields["base_url"]) != destination(existing.base_url):
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
        return {"ok": False, "error": describe(exc), "endpoints": 0}
    return await _probe_record(record)


# --- stacks -------------------------------------------------------------------


DASHBOARD_CONCURRENCY = 6


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
        result.update(reachable=False, error=describe(exc))
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
    # What could not be read is reported as such — never as "nothing there".
    environment_errors: dict[str, str] = {}
    try:
        endpoints = await client.list_endpoints()
    except Exception as exc:
        endpoints = []
        environment_errors["(all)"] = f"could not list environments: {describe(exc)}"
    for endpoint in endpoints:
        environments[endpoint["Id"]] = endpoint.get("Name") or f"env {endpoint['Id']}"

    async def containers_of(endpoint_id: int) -> tuple[int, list[dict] | None, str | None]:
        try:
            return endpoint_id, await client.list_containers(endpoint_id), None
        except Exception as exc:
            return endpoint_id, None, describe(exc)

    # Environments are independent hosts; asking them one after another
    # makes the page wait on the slowest agent times the number of agents.
    for endpoint_id, containers, error in await asyncio.gather(
        *(containers_of(e["Id"]) for e in endpoints)
    ):
        if containers is not None:
            containers_by_endpoint[endpoint_id] = containers
        else:
            environment_errors[environments[endpoint_id]] = f"could not list containers: {error}"
    result["environments"] = len(environments)
    result["environmentErrors"] = environment_errors

    owned = [
        stack_containers(stack, containers_by_endpoint.get(stack.get("EndpointId"), []))
        for stack in stacks
    ]
    # Thirty stacks at once is thirty file fetches in the same instant at one
    # Portainer; a few at a time is plenty for a page load.
    gate = asyncio.Semaphore(DASHBOARD_CONCURRENCY)

    async def images_gated(stack: dict, own: list[dict]) -> list[str]:
        async with gate:
            return await images_for(stack, own)

    image_lists = await asyncio.gather(
        *(images_gated(stack, own) for stack, own in zip(stacks, owned))
    )
    for stack, images, own in zip(stacks, image_lists, owned):
        normalized = normalize_stack(stack, images)
        normalized["containersTotal"] = len(own)
        normalized["downNames"] = [container_name(c) for c in own if container_is_down(c)]
        # Unknown is not the same as fine.
        normalized["unchecked"] = stack.get("EndpointId") not in containers_by_endpoint
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
    names_by_endpoint = stack_names_by_endpoint(stacks)

    async def standalone_row(endpoint_id: int, c: dict) -> dict:
        normalized = normalize_container(c, endpoint_id)
        async with gate:
            normalized["image"] = await resolve_image_name(client, endpoint_id, c)
        normalized["environment"] = environments.get(endpoint_id, "")
        return normalized

    result["containers"] = list(await asyncio.gather(*(
        standalone_row(endpoint_id, c)
        for endpoint_id, containers in containers_by_endpoint.items()
        for c in standalone_containers(containers, names_by_endpoint.get(endpoint_id, set()))
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


def _conflicts(key: tuple, held: tuple) -> str | None:
    """Why `key` cannot run while `held` is in flight, or None if it can.

    Keys: ("stack", iid, eid, sid), ("container", iid, eid, cid),
    ("agent", iid, eid, cid) for replacing the environment's own agent,
    ("portainer", iid) for replacing Portainer itself, ("prune", iid).
    """
    if key == held:
        return "an update is already running for this one"
    if key[1] != held[1]:
        return None  # different instances never conflict
    kinds = {key[0], held[0]}
    if "portainer" in kinds:
        return "Portainer itself is being replaced on this instance"
    if "prune" in kinds:
        return "a clean-up is running on this instance" if held[0] == "prune" \
            else "an update is running on this instance"
    if "agent" in kinds and key[2] == held[2]:
        # Every stack on an environment deploys through its agent.
        return "this environment's agent is being replaced" if held[0] == "agent" \
            else "a deploy is running on this environment"
    return None


@asynccontextmanager
async def _exclusive(request: Request, key: tuple):
    """One operation at a time per target — and none that would pull the
    ground from under another: no stack deploy while its environment's agent
    is being replaced, nothing at all while Portainer itself is, no prune
    while anything deploys.

    Portainer refuses a second deploy of the same stack while one is running,
    and answers with a generic "Unable to update stack" — which reads as a
    failure of the update rather than of the timing. A redeploy can take a
    minute while compose waits on a healthcheck, which is ample time to click
    again, or to click from another tab.
    """
    in_flight = request.app.state.in_flight
    for held in in_flight:
        reason = _conflicts(key, held)
        if reason:
            raise HTTPException(
                status_code=409,
                detail=f"Not now — {reason}. It takes a moment while Portainer waits "
                       "for the containers to come up.",
            )
    in_flight.add(key)
    try:
        yield
    finally:
        in_flight.discard(key)


# Portainer accepts a stack deploy and returns immediately, running compose in
# the background — a redeploy the API "completed" in 68ms can still be pulling
# an image a minute later. So watch the stack's containers until they settle,
# then check that what settled is actually working, and report that.
DEPLOY_POLL_SECONDS = 2.0
DEPLOY_SETTLE_POLLS = 3      # unchanged this many times running = settled
DEPLOY_TIMEOUT_SECONDS = 600.0


async def _stack_containers_now(client: PortainerClient, stack: dict) -> list[dict] | None:
    """The stack's containers, or None when the listing itself failed — which
    says nothing about the stack and must not be read as "all gone"."""
    try:
        return stack_containers(stack, await client.list_containers(stack["EndpointId"]))
    except Exception:
        return None


def _fingerprint(containers: list[dict]) -> frozenset:
    """Identity and state only. Docker's human-readable Status ("Up 5
    seconds") changes on every poll for a minute, which once made every
    deploy look like it took exactly that long to settle."""
    return frozenset((c.get("Id"), c.get("State"), c.get("ImageID")) for c in containers)


_EXIT_CODE_RE = re.compile(r"Exited \((\d+)\)")


def _container_working(c: dict) -> bool:
    """Running and not failing its healthcheck — or finished cleanly, which
    is what a one-shot service (a backup job, a migration) is meant to do."""
    state = (c.get("State") or "").lower()
    status = c.get("Status") or ""
    if state == "running":
        return "(unhealthy)" not in status
    if state == "exited":
        match = _EXIT_CODE_RE.search(status)
        return bool(match) and match.group(1) == "0"
    return False


async def _await_deploy(client: PortainerClient, stack: dict, before: frozenset) -> dict:
    """Block until the deploy settles, then judge it. Returns
    {"ok": bool, "message": str}; ok means the stack's containers were
    replaced and are all working — nothing less is called a success."""
    deadline = time.monotonic() + DEPLOY_TIMEOUT_SECONDS
    changed = False
    last = before
    last_containers: list[dict] = []
    stable = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(DEPLOY_POLL_SECONDS)
        containers = await _stack_containers_now(client, stack)
        if containers is None:
            continue
        current = _fingerprint(containers)
        if current != last:
            changed, last, last_containers, stable = True, current, containers, 0
            continue
        last_containers = containers
        if changed:
            stable += 1
            if stable >= DEPLOY_SETTLE_POLLS:
                break
    else:
        if not changed:
            return {"ok": False, "message": (
                f"Portainer accepted the redeploy, but nothing about the stack's containers "
                f"changed in {int(DEPLOY_TIMEOUT_SECONDS)}s — not confirmed.")}
        return {"ok": False, "message": (
            f"Still changing after {int(DEPLOY_TIMEOUT_SECONDS)}s — the deploy may finish in "
            "the background, but it is not confirmed. Check the stack in Portainer.")}

    if not last_containers:
        return {"ok": False, "message": "The deploy settled with no containers in the stack."}
    broken = [container_name(c) for c in last_containers if not _container_working(c)]
    if broken:
        return {"ok": False, "message": (
            f"Redeployed, but not working: {', '.join(sorted(broken))} "
            f"({'is' if len(broken) == 1 else 'are'} stopped, restarting, or unhealthy).")}
    recreated = len({c[0] for c in last} - {c[0] for c in before})
    return {"ok": True, "message": f"Redeployed {recreated} container{'' if recreated == 1 else 's'}, all running."}


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
    for held in app.state.in_flight:
        reason = _conflicts(key, held)
        if reason:
            raise HTTPException(status_code=409, detail=f"Not now — {reason}.")
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


HELPER_SOCKET = "/var/run/docker.sock"
HELPER_POLL_SECONDS = 3.0
HELPER_TIMEOUT_SECONDS = 900.0


def helper_image() -> str:
    """The same build as this dashboard, so the helper and the code that
    reads its answer can never disagree. Every build is pushed under its
    commit as well as :latest."""
    explicit = os.environ.get("RESTRUO_HELPER_IMAGE", "").strip()
    if explicit:
        return explicit
    version = os.environ.get("RESTRUO_VERSION", "dev")
    tag = version if version and version != "dev" else "latest"
    return f"ghcr.io/jwapps-app/restruo:{tag}"


def helper_spec(image: str, target: str) -> dict:
    """A container that can reach the Docker socket and nothing else: no
    network, no environment, and it replaces exactly one named container."""
    return {
        "Image": image,
        "Entrypoint": ["python", "-m", "app.helper"],
        "Cmd": [target],
        "User": "0",  # the socket is root's
        "Env": [],
        "Labels": {"restruo.helper": "1"},
        "HostConfig": {
            "Binds": [f"{HELPER_SOCKET}:{HELPER_SOCKET}"],
            "NetworkMode": "none",
            "AutoRemove": False,  # its exit status and last line are the result
        },
    }


def parse_helper_result(logs: str, exit_code: int | None) -> dict:
    for line in reversed(logs.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return {"ok": bool(parsed["ok"]), "message": str(parsed.get("message", ""))}
    tail = logs.strip().splitlines()[-1] if logs.strip() else "no output"
    return {"ok": False, "message": f"The helper exited with status {exit_code}: {tail}"}


async def _remove_helper(client: PortainerClient, endpoint_id: int, helper_id: str) -> None:
    """Remove a helper we started — and only that: the id is checked for the
    label the helper is created with before anything is deleted."""
    try:
        info = await client.get_container_info(endpoint_id, helper_id)
        labels = ((info.get("Config") or {}).get("Labels") or {})
        if labels.get("restruo.helper") != "1":
            logger.warning("Not removing %s: it does not carry the helper label", helper_id[:12])
            return
        await client.remove_container(endpoint_id, helper_id, force=True)
    except Exception as exc:
        logger.warning("Could not remove helper %s: %s", helper_id[:12], describe(exc))


async def _replace_with_helper(
    client: PortainerClient, endpoint_id: int, cid: str, name: str
) -> dict:
    image = helper_image()
    # A name of our own, unique per run: nothing is ever removed on the
    # strength of a predictable name, and two runs cannot collide.
    helper_name = f"restruo-helper-{cid[:12]}-{secrets.token_hex(3)}"
    try:
        await client.pull_image(endpoint_id, image)
        helper_id = await client.create_container(
            endpoint_id, helper_name, helper_spec(image, cid)
        )
        await client.set_container_state(endpoint_id, helper_id, running=True)
    except Exception as exc:
        return {"ok": False, "message": f"Could not start the update helper: {describe(exc)}"}

    # From here the agent — or Portainer itself — goes away and comes back, so
    # a failed poll is expected and means nothing. Only the helper's own exit
    # says how it went.
    deadline = time.monotonic() + HELPER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(HELPER_POLL_SECONDS)
        try:
            state = (await client.get_container_info(endpoint_id, helper_id)).get("State") or {}
        except Exception:
            continue
        if state.get("Running") or state.get("Status") in ("created", "restarting"):
            continue
        try:
            logs = await client.container_logs(endpoint_id, helper_id)
        except Exception:
            logs = ""
        result = parse_helper_result(logs, state.get("ExitCode"))
        await _remove_helper(client, endpoint_id, helper_id)
        return result
    return {"ok": False,
            "message": f"Lost track of the update after {int(HELPER_TIMEOUT_SECONDS / 60)} "
                       f"minutes — check “{name}” on that host."}


def describe(exc: Exception) -> str:
    return exc.message if isinstance(exc, PortainerError) else (str(exc) or type(exc).__name__)


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(request: Request, job_id: str):
    _prune_jobs(request.app.state.jobs)
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job — it may have expired.")
    return {"done": job["done"], "result": job["result"]}


async def _update_one(client: PortainerClient, stack: dict) -> dict:
    name = stack.get("Name", f"stack {stack.get('Id')}")
    started = time.monotonic()
    containers = await _stack_containers_now(client, stack)
    before = _fingerprint(containers or [])
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
            "message": describe(exc),
        }
    outcome = await _await_deploy(client, stack, before)
    return {
        "ok": outcome["ok"],
        "stack": name,
        "durationMs": int((time.monotonic() - started) * 1000),
        "message": outcome["message"],
    }


async def _find_container(
    client: PortainerClient, cid: str, endpoint_id: int
) -> tuple[int, dict]:
    """Locate a container in one named environment.

    Container ids are unique per host, not per Portainer: machines cloned from
    a template carry the same ids. Scanning environments for a bare id would
    act on whichever host happens to come first — a different machine than
    the row that was clicked — so the environment is never optional.
    """
    for container in await client.list_containers(endpoint_id):
        if container.get("Id") == cid:
            return endpoint_id, container
    raise HTTPException(
        status_code=404,
        detail=f"No container {cid[:12]} in environment {endpoint_id}",
    )


def _timed(started: float, name: str, message: str, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "stack": name,
        "durationMs": int((time.monotonic() - started) * 1000),
        "message": message,
    }


async def _stack_image_names(
    client: PortainerClient, stack: dict, name: str, verb: str
) -> list[str]:
    """What the stack's containers actually run, resolved past a moved tag.

    This feeds the guards that keep Portainer and its agents from being
    stopped or redeployed through themselves. A guard that cannot look must
    not pass — so a failed listing refuses the action rather than allowing it.
    """
    endpoint_id = stack["EndpointId"]
    try:
        own = stack_containers(stack, await client.list_containers(endpoint_id))
        return [await resolve_image_name(client, endpoint_id, c) for c in own]
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Couldn't confirm what “{name}” runs ({describe(exc)}) — refusing "
                   f"to {verb} it until that can be checked.",
        )


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
    _audit(request, "%s stack %r on instance %d", action, name, iid)
    if action == "stop":
        images = await _stack_image_names(client, stack, name, "stop")
        if any(is_self_critical_image(i) for i in images):
            raise HTTPException(
                status_code=400,
                detail=f"“{name}” runs Portainer or Restruo itself — stopping it from here "
                       "would cut off the connection needed to start it again.",
            )
    try:
        async with _exclusive(request, ("stack", iid, stack["EndpointId"], sid)):
            await client.set_stack_state(sid, stack["EndpointId"], running=action == "start")
    except HTTPException:
        raise
    except Exception as exc:
        message = describe(exc)
        return JSONResponse(status_code=502, content=_timed(started, name, message, ok=False))
    return _timed(started, name, "Started." if action == "start" else "Stopped.")


@app.post("/api/instances/{iid}/stacks/{sid}/start", dependencies=[Depends(require_auth)])
async def start_stack(request: Request, iid: int, sid: int):
    return await _set_stack_state(request, iid, sid, "start")


@app.post("/api/instances/{iid}/stacks/{sid}/stop", dependencies=[Depends(require_auth)])
async def stop_stack(request: Request, iid: int, sid: int):
    return await _set_stack_state(request, iid, sid, "stop")


async def _set_container_state(
    request: Request, iid: int, cid: str, action: str, endpoint_id: int
):
    """Start or stop a standalone container."""
    client = _get_client(request, iid)
    started = time.monotonic()
    try:
        endpoint_id, container = await _find_container(client, cid, endpoint_id)
    except HTTPException:
        raise
    except Exception as exc:
        message = describe(exc)
        raise HTTPException(status_code=502, detail=f"Could not find container: {message}")

    name = container_name(container)
    _audit(request, "%s container %r on instance %d env %d", action, name, iid, endpoint_id)
    image = await resolve_image_name(client, endpoint_id, container)
    if action == "stop" and is_self_critical_image(image):
        raise HTTPException(
            status_code=400,
            detail=f"“{name}” is Portainer or Restruo itself — stopping it from here would "
                   "cut off the connection needed to start it again.",
        )
    try:
        async with _exclusive(request, ("container", iid, endpoint_id, cid)):
            await client.set_container_state(endpoint_id, cid, running=action == "start")
    except HTTPException:
        raise
    except Exception as exc:
        message = describe(exc)
        return JSONResponse(status_code=502, content=_timed(started, name, message, ok=False))
    return _timed(started, name, "Started." if action == "start" else "Stopped.")


@app.post("/api/instances/{iid}/containers/{cid}/start", dependencies=[Depends(require_auth)])
async def start_container(
    request: Request, iid: int, cid: str, endpointId: int
):
    return await _set_container_state(request, iid, cid, "start", endpointId)


@app.post("/api/instances/{iid}/containers/{cid}/stop", dependencies=[Depends(require_auth)])
async def stop_container(
    request: Request, iid: int, cid: str, endpointId: int
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

    _audit(request, "update stack %r on instance %d", stack.get("Name", sid), iid)
    images = await _stack_image_names(client, stack, stack.get("Name", str(sid)), "redeploy")
    if any(cannot_recreate_image(i) for i in images):
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

    return await _run_job(request, ("stack", iid, stack["EndpointId"], sid), work)


@app.post("/api/instances/{iid}/containers/{cid}/update", dependencies=[Depends(require_auth)])
async def update_container(
    request: Request, iid: int, cid: str, endpointId: int
):
    """Repull + recreate a standalone container. `endpointId` is required:
    container ids are unique per host, and cloned hosts share them, so a
    bare id can name several containers on several machines."""
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

    name = container_name(container)
    _audit(request, "update container %r (%s) on instance %d env %d", name, resolved_image, iid, endpoint_id)

    if cannot_recreate_image(resolved_image):
        # Portainer's own recreate stops the container that is carrying the
        # command, so the create that should follow is never sent. Hand the
        # job to a helper the Docker daemon runs, which outlives it.
        async def helper_work() -> dict:
            result = await _replace_with_helper(client, endpoint_id, cid, name)
            result.update(stack=name,
                          durationMs=int((time.monotonic() - started) * 1000))
            if result["ok"]:
                request.app.state.checker.mark_updated(
                    iid, container_id=cid, endpoint_id=endpoint_id
                )
            return result

        kind = "portainer" if "portainer/portainer" in resolved_image.lower() else "agent"
        return await _run_job(request, (kind, iid, endpoint_id, cid), helper_work)

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
    _audit(request, "prune instance %d (images=%s all=%s networks=%s volumes=%s)",
           iid, body.images, body.allImages, body.networks, body.volumes)
    summary = {
        "ok": True, "spaceReclaimed": 0,
        "images": 0, "networks": 0, "volumes": 0, "errors": [],
    }
    try:
        endpoints = await client.list_endpoints()
    except Exception as exc:
        message = describe(exc)
        raise HTTPException(status_code=502, detail=f"Could not list environments: {message}")

    def _msg(exc: Exception) -> str:
        return describe(exc)

    async with _exclusive(request, ("prune", iid)):
        await _prune_endpoints(client, endpoints, body, summary, _msg)
    summary["ok"] = not summary["errors"]
    return summary


async def _prune_endpoints(client, endpoints, body, summary, _msg) -> None:
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


# --- updates & UI -------------------------------------------------------------


@app.get("/api/updates", dependencies=[Depends(require_auth)])
async def get_updates(request: Request):
    return request.app.state.checker.snapshot()


@app.post("/api/check-updates", dependencies=[Depends(require_auth)])
async def check_updates(request: Request):
    # Asked for by hand, so the answer is on screen — no mail for it.
    return await request.app.state.checker.check_all(notify=False)


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
