"""Cookie sessions for the dashboard.

The login form exchanges username/password for a signed, expiring session
token in an HttpOnly cookie, so PWA/standalone use doesn't depend on the
browser's basic-auth dialog (which is slow and doesn't survive force close on
iOS). The signing secret persists in the data directory so sessions survive
container updates. Basic auth keeps working alongside for curl/scripts.
"""

import hashlib
import hmac
import secrets
import time
from pathlib import Path

SESSION_COOKIE = "restruo_session"
SESSION_TTL_SECONDS = 30 * 86400


class SessionManager:
    def __init__(self, secret_path: Path):
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if secret_path.is_file():
            self._secret = secret_path.read_bytes()
        else:
            self._secret = secrets.token_bytes(32)
            secret_path.write_bytes(self._secret)

    def _sign(self, expiry: str) -> str:
        return hmac.new(self._secret, expiry.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expiry = str(int(time.time()) + SESSION_TTL_SECONDS)
        return f"{expiry}.{self._sign(expiry)}"

    def verify(self, token: str) -> bool:
        expiry, _, signature = token.partition(".")
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        return hmac.compare_digest(signature, self._sign(expiry))
