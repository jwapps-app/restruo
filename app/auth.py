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

The second half of this module is the HTTP side: the dependency every data
endpoint sits behind, and the checks it is made of. It reads its state from
request.app.state (config, sessions, limiter), which the lifespan sets up.
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger("restruo.auth")

SESSION_COOKIE = "restruo_session"
SESSION_TTL_SECONDS = 30 * 86400
# expiry(10 digits) . nonce(16 hex) . signature(64 hex) — anything longer is
# not ours. Bounding it also keeps int() from choking on a pathological cookie.
MAX_TOKEN_LENGTH = 128
# Exactly what issue() produces. Anything else is not ours and is not parsed
# — isdigit() accepts digits int() does not, and compare_digest wants ASCII.
TOKEN_RE = re.compile(r"\A([0-9]{10,12})\.([0-9a-f]{16})\.([0-9a-f]{64})\Z")


class SessionManager:
    def __init__(self, secret_path: Path, password: str | None = None):
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if secret_path.is_file():
            secret = secret_path.read_bytes()
        else:
            secret = secrets.token_bytes(32)
            # Created private — never world-readable for the instant before a chmod.
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secret)
        secret_path.chmod(0o600)
        self._key = hmac.new(secret, (password or "").encode(), hashlib.sha256).digest()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expiry = str(int(time.time()) + SESSION_TTL_SECONDS)
        payload = f"{expiry}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> bool:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
            return False
        match = TOKEN_RE.match(token)
        if match is None:
            return False
        expiry, nonce, signature = match.groups()
        if int(expiry) < time.time():
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
            if len(self._failures) > 512:
                # Addresses are only expired when revisited; sweep the rest so
                # a scan from many addresses cannot grow this without bound.
                for addr_ in list(self._failures):
                    self._recent(addr_, now)  # drops the address when nothing recent remains

    def reset(self, addr: str) -> None:
        with self._lock:
            self._failures.pop(addr, None)


# --- the HTTP side ------------------------------------------------------------

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# A cookie-authenticated request that changes something must carry this
# header. A form or a no-cors fetch from another page cannot add a custom
# header, so it cannot ride the session cookie — which SameSite=Lax alone does
# not prevent from a page on another port of the same host. Basic-auth callers
# (curl, scripts) never carry the cookie and are unaffected.
CSRF_HEADER = "X-Restruo"

basic_credentials = HTTPBasic(auto_error=False)


def client_addr(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "").lower() == "https"


def credentials_valid(request: Request, username: str, password: str) -> bool:
    auth = request.app.state.config.ui.auth
    return (
        secrets.compare_digest(username.encode(), auth.username.encode())
        and secrets.compare_digest(password.encode(), (auth.password or "").encode())
    )


def refuse_if_blocked(request: Request) -> None:
    if request.app.state.limiter.blocked(client_addr(request)):
        raise HTTPException(
            status_code=429, detail="Too many failed logins — try again later."
        )


def check_login(request: Request, username: str, password: str) -> bool:
    """One password attempt, counted: a failure spends from the address's
    budget and is logged with where it came from; a success clears it.
    Raises 429 once the budget is gone."""
    refuse_if_blocked(request)
    limiter: LoginLimiter = request.app.state.limiter
    addr = client_addr(request)
    if credentials_valid(request, username, password):
        limiter.reset(addr)
        return True
    limiter.record_failure(addr)
    logger.warning("Failed login for %r from %s", username, addr)
    return False


def session_valid(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token) and request.app.state.sessions.verify(token)


def require_csrf_header(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(
            status_code=403,
            detail=f"Missing {CSRF_HEADER} header — a browser session must send it "
                   "on every request that changes something.",
        )


def _basic_auth_ok(request: Request, credentials: HTTPBasicCredentials | None) -> bool:
    if credentials is None:
        return False
    return check_login(request, credentials.username, credentials.password)


def require_auth(
    request: Request, credentials: HTTPBasicCredentials | None = Depends(basic_credentials)
):
    if not request.app.state.config.ui.auth.enabled:
        return
    if session_valid(request):
        require_csrf_header(request)
        return
    if _basic_auth_ok(request, credentials):
        return
    # No WWW-Authenticate header: the app has its own login form, and the
    # header would make browsers pop the (slow) native basic-auth dialog.
    raise HTTPException(status_code=401, detail="Unauthorized")


def is_authenticated(request: Request, credentials: HTTPBasicCredentials | None) -> bool:
    """Like require_auth, but a question rather than a gate."""
    if not request.app.state.config.ui.auth.enabled:
        return True
    if session_valid(request):
        return True
    try:
        return _basic_auth_ok(request, credentials)
    except HTTPException:
        return False
