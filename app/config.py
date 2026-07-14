"""Configuration loading for Restruo.

The YAML config file is OPTIONAL. Without one, sensible defaults apply: auth
enabled (username RESTRUO_USERNAME or "admin", password from
DASHBOARD_PASSWORD), title from RESTRUO_TITLE, update checks every 6 hours.
A file at /config/config.yaml (override with CONFIG_PATH) overrides those.
Passwords are never stored in the file — the config names an environment
variable and the value is read from the process env.
"""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class InstanceConfig(BaseModel):
    name: str
    base_url: str
    api_key: str
    verify_tls: bool = True

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class AuthConfig(BaseModel):
    enabled: bool = True
    username: str = Field(default_factory=lambda: os.environ.get("RESTRUO_USERNAME", "admin"))
    password_env: str = "DASHBOARD_PASSWORD"

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env)


class UIConfig(BaseModel):
    title: str = Field(default_factory=lambda: os.environ.get("RESTRUO_TITLE", "Restruo"))
    auth: AuthConfig = Field(default_factory=AuthConfig)


def _floating_tags_default() -> list[str]:
    raw = os.environ.get("RESTRUO_FLOATING_TAGS", "latest")
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


class UpdatesConfig(BaseModel):
    enabled: bool = True
    interval_hours: float = Field(default=6, gt=0)
    # Tags treated as "floating" (checked against the registry). Anything else
    # is considered pinned. Some projects use rolling tags besides latest,
    # e.g. immich's :release.
    floating_tags: list[str] = Field(default_factory=_floating_tags_default)


class AppConfig(BaseModel):
    # Optional seed list: imported into the instance store on first start,
    # then managed from the settings UI.
    instances: list[InstanceConfig] = Field(default_factory=list)
    ui: UIConfig = Field(default_factory=UIConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)


DEFAULT_CONFIG_PATH = "/config/config.yaml"


def load_config(path: str | None = None) -> AppConfig:
    config_path = Path(path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    raw = {}
    if config_path.is_file():
        with config_path.open() as f:
            raw = yaml.safe_load(f) or {}
    config = AppConfig.model_validate(raw)

    if config.ui.auth.enabled and not config.ui.auth.password:
        raise ValueError(
            f"Dashboard auth is enabled but the environment variable "
            f"'{config.ui.auth.password_env}' is not set. Set it, or disable "
            "auth via a config file (ui.auth.enabled: false)."
        )
    return config
