"""A Portainer agent can't be recreated through Portainer.

Portainer relays every command for an agent environment through that agent, so
a recreate stops the container carrying the command and the replacement is
never created. The environment is left offline with the new image pulled and
unused, and Portainer can no longer reach that machine to finish or undo it —
the same shape as Portainer updating its own container.
"""
import pytest

from app.portainer import cannot_recreate_image, is_self_critical_image


@pytest.mark.parametrize("image", [
    "portainer/agent:latest",
    "portainer/agent:lts",
    "portainer/agent@sha256:" + "a" * 64,
    "PORTAINER/AGENT:latest",
    "portainer/portainer-ce:lts",
])
def test_images_that_cannot_be_recreated(image):
    assert cannot_recreate_image(image)
    # Stopping them is equally unrecoverable from here.
    assert is_self_critical_image(image)


@pytest.mark.parametrize("image", [
    "amir20/dozzle:latest",
    "prom/node-exporter:latest",
    "ghcr.io/jwapps-app/vocalis-api:latest",
    "portainer-backup/something:latest",
])
def test_ordinary_images_are_untouched(image):
    assert not cannot_recreate_image(image)


def test_restruo_may_still_update_itself():
    """Recreating Restruo is disruptive but completes — Portainer performs it,
    so nothing severs the connection doing the work."""
    assert not cannot_recreate_image("ghcr.io/jwapps-app/restruo:latest")
    # ...but it still must not be stopped from its own UI.
    assert is_self_critical_image("ghcr.io/jwapps-app/restruo:latest")
