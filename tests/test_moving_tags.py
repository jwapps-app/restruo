"""Channel tags move; version tags don't.

Restruo checked only `latest` and filed everything else as a deliberate pin.
Tags like `lts`, `stable` and `main` name a channel, not a version — they move
under you exactly like `latest` — so they were silently never checked. Being
quiet about something that moved is the worst way for an update checker to be
wrong.
"""
import os

import pytest

from app.config import MOVING_TAGS, AppConfig, load_config


@pytest.mark.parametrize("tag", [
    "latest", "lts", "stable", "release", "edge", "main", "master", "nightly",
])
def test_channel_tags_are_checked_by_default(tag, monkeypatch):
    monkeypatch.delenv("RESTRUO_FLOATING_TAGS", raising=False)
    assert tag in AppConfig().updates.floating_tags


@pytest.mark.parametrize("tag", [
    "16", "16-alpine", "7-alpine", "2026.07.2", "3.12-slim", "18",
])
def test_version_tags_stay_pinned(tag, monkeypatch):
    """A tag naming a version is a choice the operator made — leave it alone."""
    monkeypatch.delenv("RESTRUO_FLOATING_TAGS", raising=False)
    assert tag not in AppConfig().updates.floating_tags


def test_every_default_is_digit_free():
    """The line between the two lists: a digit means a version was chosen."""
    for tag in MOVING_TAGS.split(","):
        assert not any(c.isdigit() for c in tag), tag


def test_explicit_setting_still_wins(monkeypatch):
    monkeypatch.setenv("RESTRUO_FLOATING_TAGS", "latest,mychannel")
    tags = AppConfig().updates.floating_tags
    assert tags == ["latest", "mychannel"]
    assert "lts" not in tags
