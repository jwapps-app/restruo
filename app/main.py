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
from pydantic import BaseModel

from .auth import SESSION_COOKIE, SESSION_TTL_SECONDS, SessionManager
from .config import AppConfig, load_config
from .instances import ClientManager, InstanceRecord, InstanceStore
from .notifiers import build_notifiers
from .portainer import (
    PortainerClient,
    PortainerError,
    container_is_down,
    container_name,
    extract_images,
    normalize_container,
    normalize_stack,
    resolve_image_name,
    stack_containers,
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
    app.state.sessions = SessionManager(store.path.parent / "session_secret")

    app.state.registry = RegistryClient()
    app.state.checker = UpdateChecker(
        manager.items,
        app.state.registry,
        interval_hours=config.updates.interval_hours,
        notifiers=build_notifiers(config),
        floating_tags=config.updates.floating_tags,
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
async def no_store_api_responses(request: Request, call_next):
    """Live state must never be served from a browser cache — a stale
    'unreachable' would outlive the outage that caused it."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

_basic = HTTPBasic(auto_error=False)


def _credentials_valid(request: Request, username: str, password: str) -> bool:
    auth = request.app.state.config.ui.auth
    return (
        secrets.compare_digest(username.encode(), auth.username.encode())
        and secrets.compare_digest(password.encode(), (auth.password or "").encode())
    )


def require_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)):
    auth = request.app.state.config.ui.auth
    if not auth.enabled:
        return
    token = request.cookies.get(SESSION_COOKIE)
    if token and request.app.state.sessions.verify(token):
        return
    if credentials is not None and _credentials_valid(
        request, credentials.username, credentials.password
    ):
        return
    # No WWW-Authenticate header: the app has its own login form, and the
    # header would make browsers pop the (slow) native basic-auth dialog.
    raise HTTPException(status_code=401, detail="Unauthorized")


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(request: Request, body: LoginRequest):
    auth = request.app.state.config.ui.auth
    if auth.enabled and not _credentials_valid(request, body.username, body.password):
        await asyncio.sleep(1)  # blunt the speed of brute-force attempts
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.sessions.issue(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/api/logout")
async def logout():
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
        except Exception as exc:
            entry.update(reachable=False, error=str(exc))
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
        return result
    except Exception as exc:
        result.update(reachable=False, error=str(exc))
        return result

    async def images_for(stack: dict) -> list[str]:
        try:
            return extract_images(await client.get_stack_file(stack["Id"]))
        except Exception:
            return []

    containers_by_endpoint: dict[int, list[dict]] = {}
    try:
        for endpoint in await client.list_endpoints():
            try:
                containers_by_endpoint[endpoint["Id"]] = await client.list_containers(
                    endpoint["Id"]
                )
            except Exception:
                pass
    except Exception:
        pass

    image_lists = await asyncio.gather(*(images_for(stack) for stack in stacks))
    for stack, images in zip(stacks, image_lists):
        normalized = normalize_stack(stack, images)
        own = stack_containers(stack, containers_by_endpoint.get(stack.get("EndpointId"), []))
        normalized["containersTotal"] = len(own)
        normalized["downNames"] = [container_name(c) for c in own if container_is_down(c)]
        result["stacks"].append(normalized)

    # Containers that live outside any Portainer stack.
    stack_names = {s.get("Name") for s in stacks}
    for endpoint_id, containers in containers_by_endpoint.items():
        for c in standalone_containers(containers, stack_names):
            normalized = normalize_container(c, endpoint_id)
            normalized["image"] = await resolve_image_name(client, endpoint_id, c)
            result["containers"].append(normalized)
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


async def _update_one(client: PortainerClient, stack: dict) -> dict:
    name = stack.get("Name", f"stack {stack.get('Id')}")
    started = time.monotonic()
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
    return {
        "ok": True,
        "stack": name,
        "durationMs": int((time.monotonic() - started) * 1000),
        "message": "Repulled and redeployed.",
    }


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

    result = await _update_one(client, stack)
    if result["ok"]:
        request.app.state.checker.mark_updated(iid, stack_id=sid)
    status = 200 if result["ok"] else 502
    return JSONResponse(status_code=status, content=result)


@app.post("/api/instances/{iid}/containers/{cid}/update", dependencies=[Depends(require_auth)])
async def update_container(request: Request, iid: int, cid: str):
    """Repull + recreate a standalone container via Portainer's recreate action."""
    client = _get_client(request, iid)
    started = time.monotonic()
    try:
        endpoints = await client.list_endpoints()
        target = None
        for endpoint in endpoints:
            for container in await client.list_containers(endpoint["Id"]):
                if container.get("Id") == cid:
                    target = (endpoint["Id"], container)
                    break
            if target:
                break
        if target is None:
            raise HTTPException(status_code=404, detail=f"No container {cid[:12]} on this instance")
        endpoint_id, container = target
        resolved_image = await resolve_image_name(client, endpoint_id, container)
        if "portainer/portainer" in resolved_image:
            # Portainer dies the moment it stops itself, before the replacement
            # is created — the recreate can never complete. Refuse.
            raise HTTPException(
                status_code=400,
                detail="Portainer can't recreate itself through its own API — "
                       "update the Portainer container from the host instead.",
            )
        await client.recreate_container(endpoint_id, cid)
        request.app.state.checker.mark_updated(iid, container_id=cid)
    except HTTPException:
        raise
    except Exception as exc:
        message = exc.message if isinstance(exc, PortainerError) else str(exc)
        return JSONResponse(status_code=502, content={
            "ok": False,
            "stack": cid[:12],
            "durationMs": int((time.monotonic() - started) * 1000),
            "message": message,
        })
    names = container.get("Names") or []
    name = names[0].lstrip("/") if names else cid[:12]
    return {
        "ok": True,
        "stack": name,
        "durationMs": int((time.monotonic() - started) * 1000),
        "message": "Repulled and recreated.",
    }


class PruneRequest(BaseModel):
    images: bool = True
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
                pruned = await client.prune_images(endpoint_id)
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
@app.get("/api/ui-config")
async def ui_config(request: Request):
    return {
        "title": request.app.state.config.ui.title,
        "version": os.environ.get("RESTRUO_VERSION", "dev"),
        "authEnabled": request.app.state.config.ui.auth.enabled,
        "refreshSeconds": request.app.state.config.ui.refresh_seconds,
    }


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
