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

from pydantic import BaseModel, field_validator, model_validator

from .portainer import PortainerClient

DEFAULT_DATA_PATH = "/data/instances.json"


class InstanceRecord(BaseModel):
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
        return v.rstrip("/")

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


class InstanceStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("DATA_PATH", DEFAULT_DATA_PATH))
        self._lock = asyncio.Lock()
        self._records: list[InstanceRecord] = []
        if self.path.is_file():
            data = json.loads(self.path.read_text() or "[]")
            self._records = [InstanceRecord.model_validate(r) for r in data]

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def list(self) -> list[InstanceRecord]:
        return list(self._records)

    def get(self, iid: int) -> InstanceRecord | None:
        return next((r for r in self._records if r.id == iid), None)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([r.model_dump() for r in self._records], indent=2))
        tmp.replace(self.path)

    def _next_id(self) -> int:
        return max((r.id for r in self._records), default=0) + 1

    async def add(self, fields: dict) -> InstanceRecord:
        async with self._lock:
            record = InstanceRecord.model_validate({**fields, "id": self._next_id()})
            self._records.append(record)
            self._save()
            return record

    async def update(self, iid: int, fields: dict) -> InstanceRecord | None:
        async with self._lock:
            existing = self.get(iid)
            if existing is None:
                return None
            merged = existing.model_dump()
            # Blank/absent secrets mean "keep the stored one".
            for key, value in fields.items():
                if key in ("api_key", "password") and not value:
                    continue
                merged[key] = value
            record = InstanceRecord.model_validate({**merged, "id": iid})
            self._records[self._records.index(existing)] = record
            self._save()
            return record

    async def delete(self, iid: int) -> bool:
        async with self._lock:
            existing = self.get(iid)
            if existing is None:
                return False
            self._records.remove(existing)
            self._save()
            return True

    async def seed(self, records: list[dict]) -> None:
        """One-time import (e.g. from config.yaml) — only when no store file exists."""
        async with self._lock:
            for fields in records:
                self._records.append(
                    InstanceRecord.model_validate({**fields, "id": self._next_id()})
                )
            self._save()


class ClientManager:
    """Keeps one PortainerClient per stored instance; rebuilt after any change."""

    def __init__(self, store: InstanceStore):
        self.store = store
        self._clients: dict[int, PortainerClient] = {}

    def items(self) -> list[tuple[int, PortainerClient]]:
        return [
            (r.id, self._clients[r.id]) for r in self.store.list() if r.id in self._clients
        ]

    def get(self, iid: int) -> PortainerClient | None:
        return self._clients.get(iid)

    async def refresh(self) -> None:
        old = self._clients
        self._clients = {r.id: PortainerClient(r) for r in self.store.list()}
        for client in old.values():
            await client.aclose()

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients = {}
