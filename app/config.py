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


def _refresh_seconds_default() -> int:
    try:
        return max(0, int(os.environ.get("RESTRUO_REFRESH_SECONDS", "180")))
    except ValueError:
        return 180


class UIConfig(BaseModel):
    title: str = Field(default_factory=lambda: os.environ.get("RESTRUO_TITLE", "Restruo"))
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # Auto-refresh cadence for the open dashboard (stack/container state only,
    # never registry scans). 0 disables.
    refresh_seconds: int = Field(default_factory=_refresh_seconds_default)


# Tags that name a channel rather than a version, so they move under you.
# A tag containing a digit is a version the author chose to pin to
# (postgres:16-alpine, app:2026.07.2) and is deliberately left alone.
MOVING_TAGS = "latest,lts,stable,release,edge,main,master,nightly,rolling,dev"


def _floating_tags_default() -> list[str]:
    raw = os.environ.get("RESTRUO_FLOATING_TAGS", MOVING_TAGS)
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def _registry_auth_default() -> dict[str, str]:
    """RESTRUO_REGISTRY_AUTH="ghcr.io=user:token,registry.lan=user:pw" — logins
    used when checking private images for updates."""
    out: dict[str, str] = {}
    for entry in os.environ.get("RESTRUO_REGISTRY_AUTH", "").split(","):
        host, _, creds = entry.strip().partition("=")
        if host and ":" in creds:
            out[host] = creds
    return out


class UpdatesConfig(BaseModel):
    enabled: bool = True
    interval_hours: float = Field(default=6, gt=0)
    # Tags treated as "floating" (checked against the registry). Anything else
    # is considered pinned. Some projects use rolling tags besides latest,
    # e.g. immich's :release.
    floating_tags: list[str] = Field(default_factory=_floating_tags_default)
    # host -> "username:token"
    registry_auth: dict[str, str] = Field(default_factory=_registry_auth_default)


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Setting a server by hand is the boring part, and the address already says
# which one it is. Ports are 587/STARTTLS for all of these.
SMTP_HOSTS = {
    "gmail.com": "smtp.gmail.com",
    "googlemail.com": "smtp.gmail.com",
    "outlook.com": "smtp-mail.outlook.com",
    "hotmail.com": "smtp-mail.outlook.com",
    "live.com": "smtp-mail.outlook.com",
    "yahoo.com": "smtp.mail.yahoo.com",
    "icloud.com": "smtp.mail.me.com",
    "me.com": "smtp.mail.me.com",
    "fastmail.com": "smtp.fastmail.com",
}


def _smtp_host_default() -> str:
    """An explicit host wins; otherwise infer it from the account's domain, so
    a known provider needs only an address and password."""
    explicit = _env_str("RESTRUO_SMTP_HOST")
    if explicit:
        return explicit
    domain = _env_str("RESTRUO_SMTP_USER").rpartition("@")[2].lower()
    return SMTP_HOSTS.get(domain, "")


def _recipients_default() -> list[str]:
    """Default to mailing yourself — the usual case for a homelab."""
    listed = [a.strip() for a in _env_str("RESTRUO_EMAIL_TO").split(",") if a.strip()]
    if listed:
        return listed
    account = _env_str("RESTRUO_SMTP_USER")
    return [account] if "@" in account else []


class EmailConfig(BaseModel):
    """Outbound-only notifications: nothing has to be exposed to send mail."""
    host: str = Field(default_factory=_smtp_host_default)
    port: int = Field(default_factory=lambda: int(_env_str("RESTRUO_SMTP_PORT", "587") or 587))
    username: str = Field(default_factory=lambda: _env_str("RESTRUO_SMTP_USER"))
    password_env: str = "RESTRUO_SMTP_PASSWORD"
    sender: str = Field(default_factory=lambda: _env_str("RESTRUO_EMAIL_FROM"))
    recipients: list[str] = Field(default_factory=_recipients_default)
    # starttls (587, the usual), ssl (465), or none.
    security: str = Field(default_factory=lambda: _env_str("RESTRUO_SMTP_SECURITY", "starttls"))

    @property
    def password(self) -> str:
        return os.environ.get(self.password_env, "")

    @property
    def from_address(self) -> str:
        return self.sender or self.username

    @property
    def configured(self) -> bool:
        return bool(self.host and self.recipients and self.from_address)


class AppConfig(BaseModel):
    # Optional seed list: imported into the instance store on first start,
    # then managed from the settings UI.
    instances: list[InstanceConfig] = Field(default_factory=list)
    ui: UIConfig = Field(default_factory=UIConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


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
