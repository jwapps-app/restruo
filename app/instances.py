"""Persistent store for Portainer instances, managed from the settings UI.

Instances live in a JSON file on a writable volume (default /data/instances.json,
override with DATA_PATH). Each instance authenticates with either an API token
or a username/password. Secrets are stored server-side only and never returned
by the API.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .config import check_base_url
from .portainer import PortainerClient

DEFAULT_DATA_PATH = "/data/instances.json"
# Longer than any one job can run (a helper gets fifteen minutes), so a
# replaced client is never closed under something still using it.
RETIRE_AFTER_SECONDS = 20 * 60


def destination(url: str) -> str:
    """Scheme and host:port — what decides where a credential is sent. The
    scheme matters: https to http on the same host is a different place."""
    parts = urlsplit((url or "").strip())
    if parts.netloc:
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    return (url or "").strip().lower()


class InstanceRecord(BaseModel):
    # Validation errors are shown to the user; without this pydantic quotes
    # the offending input in them, which for this model is a token or password.
    model_config = ConfigDict(hide_input_in_errors=True)

    id: int
    name: str
    base_url: str
    verify_tls: bool = True
    auth_type: Literal["api_key", "credentials"] = "api_key"
    api_key: str | None = None
    username: str | None = None
    password: str | None = None

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return check_base_url(v)

    @model_validator(mode="after")
    def check_auth_fields(self):
        if self.auth_type == "api_key" and not self.api_key:
            raise ValueError("auth_type 'api_key' requires api_key")
        if self.auth_type == "credentials" and not (self.username and self.password):
            raise ValueError("auth_type 'credentials' requires username and password")
        return self

    def public(self) -> dict:
        """Shape safe to return to the browser — no secrets."""
        return {
            "id": self.id,
            "name": self.name,
            "baseUrl": self.base_url,
            "verifyTls": self.verify_tls,
            "authType": self.auth_type,
            "username": self.username if self.auth_type == "credentials" else None,
        }


def _write_private(path: Path, text: str) -> None:
    """Write a file nobody else can read, from the first byte: created 0600,
    never 0644-then-chmod. Flushed to disk before it is renamed into place."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # in case the file already existed
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


class InstanceStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("DATA_PATH", DEFAULT_DATA_PATH))
        self._lock = asyncio.Lock()
        self._records: list[InstanceRecord] = []
        self._next = 1
        if self.path.is_file():
            # The file holds Portainer credentials — keep it owner-only, and
            # repair permissions on files written by older versions.
            self.path.chmod(0o600)
            data = json.loads(self.path.read_text() or "[]")
            if isinstance(data, dict):
                records, self._next = data.get("instances", []), int(data.get("nextId", 1))
            else:
                records = data  # the older, bare-list format
            self._records = [InstanceRecord.model_validate(r) for r in records]
            self._next = max(self._next, max((r.id for r in self._records), default=0) + 1)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def list(self) -> list[InstanceRecord]:
        return list(self._records)

    def get(self, iid: int) -> InstanceRecord | None:
        return next((r for r in self._records if r.id == iid), None)

    def _save(self, records: list[InstanceRecord], next_id: int) -> None:
        """Persist, then publish: the in-memory state only changes once the
        file is safely on disk, so a failed write leaves memory, disk and the
        running clients agreeing with each other."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"nextId": next_id, "instances": [r.model_dump() for r in records]}
        _write_private(tmp, json.dumps(payload, indent=2))
        tmp.replace(self.path)
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # the rename is already durable on most filesystems
        self._records, self._next = records, next_id

    async def add(self, fields: dict) -> InstanceRecord:
        async with self._lock:
            # Ids are never reused: a tab still showing a deleted instance
            # must not be able to act on whatever took its number.
            record = InstanceRecord.model_validate({**fields, "id": self._next})
            self._save(self._records + [record], self._next + 1)
            return record

    async def update(self, iid: int, fields: dict) -> InstanceRecord | None:
        async with self._lock:
            existing = self.get(iid)
            if existing is None:
                return None
            merged = existing.model_dump()
            # Blank/absent secrets mean "keep the stored one" — but only for
            # the address it was stored for. A saved credential is never sent
            # anywhere the person did not type it for.
            new_url = fields.get("base_url") or existing.base_url
            auth_type = fields.get("auth_type") or existing.auth_type
            supplied = fields.get("api_key") if auth_type == "api_key" else fields.get("password")
            if destination(new_url) != destination(existing.base_url) and not supplied:
                raise ValueError(
                    "The address changed — enter the API key or password again, so the "
                    "stored one is never sent somewhere it was not saved for."
                )
            for key, value in fields.items():
                if key in ("api_key", "password") and not value:
                    continue
                merged[key] = value
            if auth_type != existing.auth_type:
                # Switching how an instance authenticates retires the other
                # credential; it must not linger on disk unused.
                merged["api_key" if auth_type == "credentials" else "password"] = None
                if auth_type == "api_key":
                    merged["username"] = None
            record = InstanceRecord.model_validate({**merged, "id": iid})
            records = [record if r.id == iid else r for r in self._records]
            self._save(records, self._next)
            return record

    async def move(self, iid: int, direction: str) -> bool:
        """Shift one instance up or down. Stored order is display order."""
        async with self._lock:
            record = self.get(iid)
            if record is None:
                return False
            records = list(self._records)
            index = records.index(record)
            target = index - 1 if direction == "up" else index + 1
            if not 0 <= target < len(records):
                return False  # already at the end
            records[index], records[target] = records[target], records[index]
            self._save(records, self._next)
            return True

    async def delete(self, iid: int) -> bool:
        async with self._lock:
            if self.get(iid) is None:
                return False
            self._save([r for r in self._records if r.id != iid], self._next)
            return True

    async def seed(self, records: list[dict]) -> None:
        """One-time import (e.g. from config.yaml) — only when no store file exists."""
        async with self._lock:
            new, next_id = list(self._records), self._next
            for fields in records:
                new.append(InstanceRecord.model_validate({**fields, "id": next_id}))
                next_id += 1
            self._save(new, next_id)


class ClientManager:
    """Keeps one PortainerClient per stored instance; rebuilt after any change."""

    def __init__(self, store: InstanceStore):
        self.store = store
        self._clients: dict[int, PortainerClient] = {}
        self._retiring: set[asyncio.Task] = set()

    def items(self) -> list[tuple[int, PortainerClient]]:
        return [
            (r.id, self._clients[r.id]) for r in self.store.list() if r.id in self._clients
        ]

    def get(self, iid: int) -> PortainerClient | None:
        return self._clients.get(iid)

    async def refresh(self) -> None:
        """Rebuild clients whose record changed; keep the rest.

        A client holds a logged-in session and a CSRF token for its Portainer.
        Editing one instance used to throw all of them away, so every other
        instance had to log in again on its next poll. A client that is
        replaced is retired, not closed: a deploy or a check that already
        holds it finishes on it, and it is closed once that could be over.
        """
        old = self._clients
        fresh: dict[int, PortainerClient] = {}
        for record in self.store.list():
            existing = old.get(record.id)
            if existing is not None and existing.instance == record:
                fresh[record.id] = existing
            else:
                fresh[record.id] = PortainerClient(record)
        self._clients = fresh
        for iid, client in old.items():
            if fresh.get(iid) is not client:
                self._retire(client)

    def _retire(self, client: PortainerClient) -> None:
        task = asyncio.create_task(self._close_later(client))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _close_later(self, client: PortainerClient) -> None:
        try:
            await asyncio.sleep(RETIRE_AFTER_SECONDS)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    async def aclose(self) -> None:
        for task in list(self._retiring):
            task.cancel()
        for task in list(self._retiring):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for client in self._clients.values():
            await client.aclose()
        self._clients = {}
