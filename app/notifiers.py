"""Notification paths for newly-available image updates.

The dashboard badge reads checker state directly; everything else goes through
the Notifier interface. New paths (ntfy, webhooks, email, …) are added by
implementing Notifier and appending to build_notifiers().
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("restruo.updates")


@dataclass(frozen=True)
class UpdateEvent:
    instance_name: str
    stack_name: str
    image: str


class Notifier(ABC):
    @abstractmethod
    async def send(self, events: list[UpdateEvent]) -> None: ...


class LogNotifier(Notifier):
    async def send(self, events: list[UpdateEvent]) -> None:
        for event in events:
            logger.info(
                "Update available: %s (stack '%s' on %s)",
                event.image, event.stack_name, event.instance_name,
            )


def build_notifiers(config) -> list[Notifier]:
    # Future: read config.updates and append NtfyNotifier / WebhookNotifier here.
    return [LogNotifier()]
