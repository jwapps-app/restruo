"""Restruo — multi-instance Portainer stack updater dashboard.

This module is the HTTP surface and the wiring. A route parses the request,
calls one function and shapes the response; what the buttons do lives in
actions.py, what may run at once in operations.py, and who may ask in auth.py.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict

from . import actions, auth, timing
from .actions import Actions
from .auth import SESSION_COOKIE, SESSION_TTL_SECONDS, LoginLimiter, SessionManager, require_auth
from .config import AppConfig, load_config
from .dashboard import instance_view
from .errors import Refused, describe
from .helper_runner import default_helper_image
from .instances import ClientManager, InstanceRecord, InstanceStore, destination
from .notifiers import EmailNotifier, UpdateEvent, build_notifiers, compose_body
from .operations import Job, Operations
from .portainer import PortainerClient
from .registry import RegistryClient
from .updates import UpdateChecker

logger = logging.getLogger("restruo")


def _configure_logging() -> None:
    """Uvicorn sets up its own loggers and nothing else, so the app's INFO
    lines — the operation log, "Emailed N updates", the instance import —
    went nowhere. Give the app's logger a handler of its own, once. The level
    is INFO until the lifespan has read the configured one."""
    root = logging.getLogger("restruo")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


_configure_logging()

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: AppConfig = getattr(app.state, "config", None) or load_config()
    app.state.config = config
    logger.setLevel(config.log_level)

    store: InstanceStore = getattr(app.state, "store", None) or InstanceStore(config.data_path)
    app.state.store = store
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

    app.state.registry = RegistryClient(credentials={
        host: (creds.split(":", 1)[0], creds.split(":", 1)[1])
        for host, creds in config.updates.registry_auth.items()
    })
    app.state.checker = UpdateChecker(
        manager.items,
        app.state.registry,
        config.updates,
        notifiers=build_notifiers(config),
        state_path=store.path.parent / "notified.json",
    )
    # What is running right now, and the jobs that outlived their request.
    app.state.ops = Operations()
    app.state.actions = Actions(
        app.state.ops,
        app.state.checker,
        helper_image=config.helper_image or default_helper_image(config.version),
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


@app.exception_handler(Refused)
async def refused(request: Request, exc: Refused):
    """An action that was not attempted, answered the way FastAPI answers an
    HTTPException: the reason in `detail`."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


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


class LoginRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    username: str
    password: str


@app.post("/api/login")
async def login(request: Request, body: LoginRequest):
    if request.app.state.config.ui.auth.enabled:
        if not auth.check_login(request, body.username, body.password):
            await asyncio.sleep(1)
            raise HTTPException(status_code=401, detail="Wrong username or password.")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.sessions.issue(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=auth.is_https(request),
    )
    return response


@app.post("/api/logout")
async def logout(request: Request):
    if auth.session_valid(request):
        auth.require_csrf_header(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


def _manager(request: Request) -> ClientManager:
    return request.app.state.manager


def _get_client(request: Request, iid: int) -> PortainerClient:
    client = _manager(request).get(iid)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No instance with id {iid}")
    return client


def _actions(request: Request) -> Actions:
    return request.app.state.actions


def _audit(request: Request, what: str, *args) -> None:
    """One line per change made through the API, with where it came from.
    Failed logins were already logged; the things a login lets you do were not."""
    logger.info("[%s] " + what, auth.client_addr(request), *args)


def _result_response(result: dict) -> JSONResponse:
    """Every action answers with the same body; the status says how it went."""
    return JSONResponse(status_code=200 if result.get("ok") else 502, content=result)


def _job_response(job: Job) -> JSONResponse:
    """The result when the work was quick, otherwise an id to ask about."""
    if not job.done:
        return JSONResponse(status_code=202, content={
            "ok": None, "jobId": job.id,
            "message": "Still deploying — Restruo keeps watching it.",
        })
    return _result_response(job.result or {"ok": False, "message": "No result recorded."})


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
        except Exception as exc:
            entry.update(reachable=False, error=describe(exc))
            logger.warning("Instance %r unreachable: %s", record.name, describe(exc))
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
        # Not a failure to report but the form's own mistake, in pydantic's words.
        return {"ok": False, "error": str(exc), "endpoints": 0}
    return await _probe_record(record)


# --- stacks -------------------------------------------------------------------


@app.get("/api/stacks", dependencies=[Depends(require_auth)])
async def list_all_stacks(request: Request):
    return await asyncio.gather(
        *(
            instance_view(iid, client.instance.name, client)
            for iid, client in _manager(request).items()
        )
    )


# --- actions --------------------------------------------------------------------


@app.post("/api/instances/{iid}/stacks/{sid}/update", dependencies=[Depends(require_auth)])
async def update_stack(request: Request, iid: int, sid: int):
    client = _get_client(request, iid)
    stack = await actions.fetch_stack(client, sid)
    _audit(request, "update stack %r on instance %d", actions.stack_name(stack), iid)
    return _job_response(await _actions(request).update_stack(client, iid, stack))


async def _set_stack_state(request: Request, iid: int, sid: int, action: str):
    client = _get_client(request, iid)
    stack = await actions.fetch_stack(client, sid)
    _audit(request, "%s stack %r on instance %d", action, actions.stack_name(stack), iid)
    return _result_response(
        await _actions(request).set_stack_state(client, iid, stack, running=action == "start")
    )


@app.post("/api/instances/{iid}/stacks/{sid}/start", dependencies=[Depends(require_auth)])
async def start_stack(request: Request, iid: int, sid: int):
    return await _set_stack_state(request, iid, sid, "start")


@app.post("/api/instances/{iid}/stacks/{sid}/stop", dependencies=[Depends(require_auth)])
async def stop_stack(request: Request, iid: int, sid: int):
    return await _set_stack_state(request, iid, sid, "stop")


@app.post("/api/instances/{iid}/containers/{cid}/update", dependencies=[Depends(require_auth)])
async def update_container(request: Request, iid: int, cid: str, endpointId: int):
    """Repull + recreate a standalone container. `endpointId` is required:
    container ids are unique per host, and cloned hosts share them, so a
    bare id can name several containers on several machines."""
    client = _get_client(request, iid)
    target = await actions.locate_container(client, cid, endpointId)
    _audit(request, "update container %r (%s) on instance %d env %d",
           target.name, target.image, iid, endpointId)
    return _job_response(await _actions(request).update_container(client, iid, target))


async def _set_container_state(
    request: Request, iid: int, cid: str, endpoint_id: int, action: str
):
    client = _get_client(request, iid)
    target = await actions.locate_container(client, cid, endpoint_id)
    _audit(request, "%s container %r on instance %d env %d",
           action, target.name, iid, endpoint_id)
    return _result_response(
        await _actions(request).set_container_state(
            client, iid, target, running=action == "start")
    )


@app.post("/api/instances/{iid}/containers/{cid}/start", dependencies=[Depends(require_auth)])
async def start_container(request: Request, iid: int, cid: str, endpointId: int):
    return await _set_container_state(request, iid, cid, endpointId, "start")


@app.post("/api/instances/{iid}/containers/{cid}/stop", dependencies=[Depends(require_auth)])
async def stop_container(request: Request, iid: int, cid: str, endpointId: int):
    return await _set_container_state(request, iid, cid, endpointId, "stop")


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(request: Request, job_id: str):
    job = request.app.state.ops.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job — it may have expired.")
    return {"done": job.done, "result": job.result}


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
    return await _actions(request).prune(
        client, iid, images=body.images, all_images=body.allImages,
        networks=body.networks, volumes=body.volumes,
    )


# --- updates & UI -------------------------------------------------------------


@app.get("/api/updates", dependencies=[Depends(require_auth)])
async def get_updates(request: Request):
    return request.app.state.checker.snapshot()


@app.post("/api/check-updates", dependencies=[Depends(require_auth)])
async def check_updates(request: Request):
    # Asked for by hand, so the answer is on screen — no mail for it.
    return await request.app.state.checker.check_all(notify=False)


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
        raise HTTPException(status_code=502, detail=describe(exc))
    return {"ok": True, "sentTo": email.recipients}


@app.get("/api/ui-config")
async def ui_config(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(auth.basic_credentials),
):
    """Title and version are cosmetic and shown on the login screen, so they
    need no auth. Who gets emailed is not public."""
    config = request.app.state.config
    out = {
        "title": config.ui.title,
        "version": config.version,
        "authEnabled": config.ui.auth.enabled,
    }
    if auth.is_authenticated(request, credentials):
        out["refreshSeconds"] = config.ui.refresh_seconds
        out["jobWaitSeconds"] = timing.PAGE_JOB_WAIT_SECONDS
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
