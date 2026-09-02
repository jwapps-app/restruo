"""Cookie sessions for the dashboard.

The login form exchanges username/password for a signed, expiring session
token in an HttpOnly cookie, so PWA/standalone use doesn't depend on the
browser's basic-auth dialog (which is slow and doesn't survive force close on
iOS). The signing secret persists in the data directory so sessions survive
container updates. Basic auth keeps working alongside for curl/scripts.

Tokens are signed with a key derived from that secret AND the current
dashboard password, so changing the password signs every device out — which
is what a password change is for. Each token also carries a random nonce, so
two logins never share a token.
"""

import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

SESSION_COOKIE = "restruo_session"
SESSION_TTL_SECONDS = 30 * 86400
# expiry(10 digits) . nonce(16 hex) . signature(64 hex) — anything longer is
# not ours. Bounding it also keeps int() from choking on a pathological cookie.
MAX_TOKEN_LENGTH = 128


class SessionManager:
    def __init__(self, secret_path: Path, password: str | None = None):
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if secret_path.is_file():
            secret = secret_path.read_bytes()
        else:
            secret = secrets.token_bytes(32)
            secret_path.write_bytes(secret)
        secret_path.chmod(0o600)
        self._key = hmac.new(secret, (password or "").encode(), hashlib.sha256).digest()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expiry = str(int(time.time()) + SESSION_TTL_SECONDS)
        payload = f"{expiry}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> bool:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        parts = token.split(".")
        if len(parts) != 3:
            return False
        expiry, nonce, signature = parts
        if not expiry.isdigit() or len(expiry) > 12 or int(expiry) < time.time():
            return False
        return hmac.compare_digest(signature, self._sign(f"{expiry}.{nonce}"))


class LoginLimiter:
    """A budget of failed password attempts per client address.

    A fixed delay per failed request slows one attacker down only if they wait
    for each answer; concurrent requests make it free. Counting failures per
    address and refusing further attempts for a while does not have that hole.
    Successful logins clear the count, so a typo does not lock anyone out.
    """

    def __init__(self, max_failures: int = 10, window_seconds: float = 15 * 60):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        # require_auth runs in a worker thread; keep the bookkeeping consistent.
        self._lock = threading.Lock()

    def _recent(self, addr: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        kept = [t for t in self._failures.get(addr, []) if t > cutoff]
        if kept:
            self._failures[addr] = kept
        else:
            self._failures.pop(addr, None)
        return kept

    def blocked(self, addr: str) -> bool:
        with self._lock:
            return len(self._recent(addr, time.time())) >= self.max_failures

    def record_failure(self, addr: str) -> None:
        with self._lock:
            now = time.time()
            self._failures[addr] = self._recent(addr, now) + [now]

    def reset(self, addr: str) -> None:
        with self._lock:
            self._failures.pop(addr, None)
