"""Every setting is declared in one place and documented everywhere it must be.

docker-compose.yml passes through only the variables it lists, so a setting
missing from that list silently does nothing for anyone deploying with it.
"""

import re
from pathlib import Path

import pytest

from app.config import AppConfig, load_config

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"\b(?:RESTRUO_[A-Z0-9_]+|DASHBOARD_PASSWORD)\b")
# Set by the image build (Dockerfile ARG GIT_SHA), never by the person deploying.
BUILD_TIME = {"RESTRUO_VERSION"}


def names_in(path: Path) -> set[str]:
    return set(NAME.findall(path.read_text()))


def settings_the_app_reads() -> set[str]:
    found: set[str] = set()
    for source in (ROOT / "app").glob("*.py"):
        found |= names_in(source)
    return found - BUILD_TIME


@pytest.mark.parametrize("document", ["docker-compose.yml", ".env.example", "README.md"])
def test_every_setting_is_passed_through_and_documented(document):
    missing = settings_the_app_reads() - names_in(ROOT / document)
    assert not missing, f"{document} does not mention {sorted(missing)}"


def test_the_environment_is_read_in_config_and_nowhere_else():
    readers = [
        source.name for source in (ROOT / "app").glob("*.py")
        if "os.environ" in source.read_text() and source.name != "config.py"
    ]
    assert readers == []


def test_settings_once_read_elsewhere_are_config_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "x")
    monkeypatch.setenv("RESTRUO_LOG_LEVEL", " debug ")
    monkeypatch.setenv("RESTRUO_HELPER_IMAGE", "mirror.lan/restruo:1")
    monkeypatch.setenv("RESTRUO_VERSION", "abc123")
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "i.json"))

    config = load_config(str(tmp_path / "missing.yaml"))

    assert config.log_level == "DEBUG"
    assert config.helper_image == "mirror.lan/restruo:1"
    assert config.version == "abc123"
    assert config.data_path == str(tmp_path / "i.json")


def test_a_mistyped_log_level_is_refused_with_a_reason(monkeypatch):
    monkeypatch.setenv("RESTRUO_LOG_LEVEL", "verbose")
    with pytest.raises(ValueError, match="RESTRUO_LOG_LEVEL"):
        AppConfig()


def test_the_helper_runs_from_this_build_unless_told_otherwise():
    from app.helper_runner import default_helper_image

    assert default_helper_image("abc123") == "ghcr.io/jwapps-app/restruo:abc123"
    assert default_helper_image("dev") == "ghcr.io/jwapps-app/restruo:latest"
