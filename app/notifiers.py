"""Notification paths for newly-available image updates.

The dashboard badge reads checker state directly; everything else goes through
the Notifier interface. Email is outbound-only, so notifications work without
exposing Restruo to anything.
"""

import asyncio
import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage

logger = logging.getLogger("restruo.updates")

SMTP_TIMEOUT = 30


@dataclass(frozen=True)
class UpdateEvent:
    instance_name: str
    stack_name: str
    image: str
    # Which environment of that Portainer it runs in, when the instance manages
    # more than one. Two hosts can run the same container name and image, so
    # without this the mail lists lines nothing distinguishes.
    environment: str | None = None


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


def compose_body(events: list[UpdateEvent]) -> str:
    """Group findings by instance so the mail reads like the dashboard."""
    by_instance: dict[str, list[UpdateEvent]] = {}
    for event in events:
        by_instance.setdefault(event.instance_name, []).append(event)

    lines = []
    for instance, found in by_instance.items():
        lines.append(instance)
        for event in sorted(
            found, key=lambda e: (e.stack_name.lower(), e.environment or "", e.image)
        ):
            where = f" [{event.environment}]" if event.environment else ""
            lines.append(f"    {event.stack_name}{where} — {event.image}")
        lines.append("")
    lines.append("Open Restruo to review and update.")
    return "\n".join(lines)


class EmailNotifier(Notifier):
    def __init__(self, config):
        self.config = config

    def _deliver(self, subject: str, body: str) -> None:
        """Blocking SMTP, run off the event loop by send()."""
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.from_address
        message["To"] = ", ".join(self.config.recipients)
        message.set_content(body)

        security = (self.config.security or "starttls").lower()
        if security == "ssl":
            server = smtplib.SMTP_SSL(
                self.config.host, self.config.port,
                timeout=SMTP_TIMEOUT, context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(self.config.host, self.config.port, timeout=SMTP_TIMEOUT)
        try:
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if self.config.username and self.config.password:
                server.login(self.config.username, self.config.password)
            server.send_message(message)
        finally:
            try:
                server.quit()
            except Exception:
                pass

    async def deliver(self, subject: str, body: str) -> None:
        await asyncio.to_thread(self._deliver, subject, body)

    async def send(self, events: list[UpdateEvent]) -> None:
        if not events or not self.config.configured:
            return
        count = len(events)
        subject = f"Restruo: {count} update{'' if count == 1 else 's'} available"
        try:
            await self.deliver(subject, compose_body(events))
            logger.info("Emailed %d update(s) to %s", count,
                        ", ".join(self.config.recipients))
        except Exception:
            # A mail failure must never break the update check itself.
            logger.exception("Could not send the update email")


def build_notifiers(config) -> list[Notifier]:
    notifiers: list[Notifier] = [LogNotifier()]
    if getattr(config, "email", None) and config.email.configured:
        notifiers.append(EmailNotifier(config.email))
    return notifiers
