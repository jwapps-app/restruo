"""Configuration loading for restack.

Config lives in a single YAML file (default /config/config.yaml, override with
CONFIG_PATH). The dashboard password is never stored in the file — the config
names an environment variable and the value is read from the process env.
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
    enabled: bool = False
    username: str = "admin"
    password_env: str = "DASHBOARD_PASSWORD"

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env)


class UIConfig(BaseModel):
    title: str = "restack"
    auth: AuthConfig = Field(default_factory=AuthConfig)


class UpdatesConfig(BaseModel):
    enabled: bool = True
    interval_hours: float = Field(default=6, gt=0)


class AppConfig(BaseModel):
    # Optional seed list: imported into the instance store on first start,
    # then managed from the settings UI.
    instances: list[InstanceConfig] = Field(default_factory=list)
    ui: UIConfig = Field(default_factory=UIConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)


DEFAULT_CONFIG_PATH = "/config/config.yaml"


def load_config(path: str | None = None) -> AppConfig:
    config_path = Path(path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            "Mount your config.yaml there or set CONFIG_PATH."
        )
    with config_path.open() as f:
        raw = yaml.safe_load(f) or {}
    config = AppConfig.model_validate(raw)

    if config.ui.auth.enabled and not config.ui.auth.password:
        raise ValueError(
            f"Dashboard auth is enabled but the environment variable "
            f"'{config.ui.auth.password_env}' is not set."
        )
    return config
