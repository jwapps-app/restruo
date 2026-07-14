"""Config loading: the YAML file is optional; env vars drive the defaults."""

import pytest

from app.config import load_config


def test_defaults_without_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("RESTRUO_USERNAME", "john")
    monkeypatch.setenv("RESTRUO_TITLE", "homelab")
    config = load_config(str(tmp_path / "missing.yaml"))
    assert config.instances == []
    assert config.ui.auth.enabled is True
    assert config.ui.auth.username == "john"
    assert config.ui.auth.password == "pw"
    assert config.ui.title == "homelab"
    assert config.updates.enabled is True
    assert config.updates.interval_hours == 6


def test_missing_password_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="DASHBOARD_PASSWORD"):
        load_config(str(tmp_path / "missing.yaml"))


def test_config_file_overrides_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("ui:\n  title: custom\n  auth:\n    enabled: false\n")
    config = load_config(str(config_file))
    assert config.ui.title == "custom"
    assert config.ui.auth.enabled is False
