"""restack — multi-instance Portainer stack updater dashboard."""

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .config import AppConfig, load_config
from .instances import ClientManager, InstanceRecord, InstanceStore
from .notifiers import build_notifiers
from .portainer import (
    PortainerClient,
    PortainerError,
    extract_images,
    normalize_container,
    normalize_stack,
    standalone_containers,
)
from .registry import RegistryClient
from .updates import UpdateChecker

logger = logging.getLogger("restack")

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

    app.state.registry = RegistryClient()
    app.state.checker = UpdateChecker(
        manager.items,
        app.state.registry,
        interval_hours=config.updates.interval_hours,
        notifiers=build_notifiers(config),
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


app = FastAPI(title="restack", lifespan=lifespan)

_basic = HTTPBasic(auto_error=False)


def require_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)):
    auth = request.app.state.config.ui.auth
    if not auth.enabled:
        return
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username.encode(), auth.username.encode())
        and secrets.compare_digest(credentials.password.encode(), (auth.password or "").encode())
    )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="restack"'},
        )


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

    image_lists = await asyncio.gather(*(images_for(stack) for stack in stacks))
    result["stacks"] = [
        normalize_stack(stack, images) for stack, images in zip(stacks, image_lists)
    ]

    # Containers that live outside any Portainer stack.
    stack_names = {s.get("Name") for s in stacks}
    try:
        for endpoint in await client.list_endpoints():
            endpoint_id = endpoint["Id"]
            try:
                containers = await client.list_containers(endpoint_id)
            except Exception:
                continue
            result["containers"].extend(
                normalize_container(c, endpoint_id)
                for c in standalone_containers(containers, stack_names)
            )
    except Exception:
        pass
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
    # Re-fetch the stack list so Env / EndpointId are current at redeploy time.
    try:
        stacks = await client.list_stacks()
    except Exception as exc:
        message = exc.message if isinstance(exc, PortainerError) else str(exc)
        raise HTTPException(status_code=502, detail=f"Could not list stacks: {message}")

    stack = next((s for s in stacks if s.get("Id") == sid), None)
    if stack is None:
        raise HTTPException(status_code=404, detail=f"No stack with id {sid} on this instance")

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


class UpdateAllRequest(BaseModel):
    instanceId: int | None = None


@app.post("/api/update-all", dependencies=[Depends(require_auth)])
async def update_all(request: Request, body: UpdateAllRequest | None = None):
    instance_filter = body.instanceId if body else None
    if instance_filter is not None:
        _get_client(request, instance_filter)  # 404 on bad id

    async def run_instance(iid: int, client: PortainerClient) -> dict:
        entry = {
            "instance": {"id": iid, "name": client.instance.name},
            "results": [],
            "error": None,
        }
        try:
            stacks = await client.list_stacks()
        except Exception as exc:
            entry["error"] = exc.message if isinstance(exc, PortainerError) else str(exc)
            return entry
        # Sequential within an instance to avoid hammering one Portainer with
        # simultaneous redeploys; instances run in parallel.
        for stack in stacks:
            outcome = await _update_one(client, stack)
            if outcome["ok"]:
                request.app.state.checker.mark_updated(iid, stack_id=stack["Id"])
            entry["results"].append(outcome)
        return entry

    targets = [
        (iid, client)
        for iid, client in _manager(request).items()
        if instance_filter is None or iid == instance_filter
    ]
    return await asyncio.gather(*(run_instance(iid, client) for iid, client in targets))


# --- updates & UI -------------------------------------------------------------


@app.get("/api/updates", dependencies=[Depends(require_auth)])
async def get_updates(request: Request):
    return request.app.state.checker.snapshot()


@app.post("/api/check-updates", dependencies=[Depends(require_auth)])
async def check_updates(request: Request):
    return await request.app.state.checker.check_all()


@app.get("/api/ui-config", dependencies=[Depends(require_auth)])
async def ui_config(request: Request):
    return {"title": request.app.state.config.ui.title}


@app.get("/icon.svg")
async def icon():
    return FileResponse(WEB_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/", dependencies=[Depends(require_auth)])
async def index():
    return FileResponse(WEB_DIR / "index.html")
