"""Email notifications, exercised against a real (in-process) SMTP server."""

import asyncio
import os
import socket
import threading

import pytest

from app.config import EmailConfig
from app.notifiers import EmailNotifier, UpdateEvent, build_notifiers, compose_body


class FakeSMTP(threading.Thread):
    """Accepts one SMTP conversation and records the message it received."""

    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.message = ""
        self.commands: list[str] = []

    def run(self) -> None:
        conn, _ = self.sock.accept()
        conn.sendall(b"220 fake ESMTP\r\n")
        collecting = False
        body: list[str] = []
        with conn:
            buffer = ""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8", "replace")
                while "\r\n" in buffer:
                    line, buffer = buffer.split("\r\n", 1)
                    if collecting:
                        if line == ".":
                            collecting = False
                            self.message = "\n".join(body)
                            conn.sendall(b"250 queued\r\n")
                        else:
                            body.append(line)
                        continue
                    self.commands.append(line.split()[0].upper() if line else "")
                    upper = line.upper()
                    if upper.startswith("EHLO") or upper.startswith("HELO"):
                        conn.sendall(b"250-fake\r\n250 SIZE 10240000\r\n")
                    elif upper.startswith("DATA"):
                        collecting = True
                        conn.sendall(b"354 go ahead\r\n")
                    elif upper.startswith("QUIT"):
                        conn.sendall(b"221 bye\r\n")
                        return
                    else:
                        conn.sendall(b"250 ok\r\n")


EVENTS = [
    UpdateEvent("DS1621+", "immich", "ghcr.io/immich-app/immich-server:release"),
    UpdateEvent("DS1621+", "adguard", "adguard/adguardhome:latest"),
    UpdateEvent("Proxmox", "grafana", "grafana/grafana:latest"),
]


def test_body_groups_by_instance_and_sorts():
    body = compose_body(EVENTS)
    assert body.index("DS1621+") < body.index("Proxmox")
    # Alphabetical within an instance.
    assert body.index("adguard") < body.index("immich")
    assert "ghcr.io/immich-app/immich-server:release" in body


async def test_sends_a_real_message():
    server = FakeSMTP()
    server.start()
    config = EmailConfig(
        host="127.0.0.1", port=server.port, username="", sender="restruo@home.lan",
        recipients=["me@example.com"], security="none",
    )
    await EmailNotifier(config).send(EVENTS)
    server.join(timeout=5)

    assert "Subject: Restruo: 3 updates available" in server.message
    assert "To: me@example.com" in server.message
    assert "From: restruo@home.lan" in server.message
    assert "adguard" in server.message
    assert "DATA" in server.commands


async def test_send_failure_never_breaks_the_check():
    """A dead mail server must not take the update check down with it."""
    config = EmailConfig(
        host="127.0.0.1", port=1, sender="a@b.c", recipients=["me@example.com"],
        security="none",
    )
    await EmailNotifier(config).send(EVENTS)  # logs, does not raise


def test_notifier_is_only_built_when_configured():
    class Config:
        email = EmailConfig(host="", recipients=[])

    assert not any(isinstance(n, EmailNotifier) for n in build_notifiers(Config()))

    class Configured:
        email = EmailConfig(host="smtp.test", sender="a@b.c", recipients=["me@example.com"])

    assert any(isinstance(n, EmailNotifier) for n in build_notifiers(Configured()))


def test_known_providers_need_only_an_address_and_password(monkeypatch):
    """Two variables should be enough for a mainstream mail account."""
    import importlib
    import app.config

    def configure(**env):
        for key in list(os.environ):
            if key.startswith(("RESTRUO_SMTP", "RESTRUO_EMAIL")):
                monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        importlib.reload(app.config)
        return app.config.EmailConfig()

    gmail = configure(RESTRUO_SMTP_USER="me@gmail.com", RESTRUO_SMTP_PASSWORD="app-pw")
    assert gmail.host == "smtp.gmail.com"
    assert gmail.port == 587 and gmail.security == "starttls"
    assert gmail.recipients == ["me@gmail.com"]  # defaults to mailing yourself
    assert gmail.from_address == "me@gmail.com"
    assert gmail.configured

    assert configure(RESTRUO_SMTP_USER="me@icloud.com").host == "smtp.mail.me.com"

    # An explicit host always wins, and an unknown domain needs one.
    assert configure(
        RESTRUO_SMTP_USER="me@gmail.com", RESTRUO_SMTP_HOST="smtp.lan"
    ).host == "smtp.lan"
    assert configure(RESTRUO_SMTP_USER="me@my-own-domain.org").configured is False

    # Explicit recipients still override the default.
    assert configure(
        RESTRUO_SMTP_USER="me@gmail.com", RESTRUO_EMAIL_TO="a@x.com, b@y.com"
    ).recipients == ["a@x.com", "b@y.com"]
    importlib.reload(app.config)
