"""The errors Restruo raises, and the one way any error is put into words.

Every message that reaches the dashboard, a job result or an update-check
detail comes from describe(), so one failure reads the same wherever it shows.
"""


class PortainerError(Exception):
    """Raised when a Portainer instance returns an error response.

    Carries Portainer's error body verbatim so bad tokens / RBAC problems are
    debuggable from the dashboard.
    """

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Portainer returned {status_code}: {message}")


class RegistryError(Exception):
    """A registry would not say what a tag points at. `status_code` is its
    HTTP answer when there was one."""

    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class Refused(Exception):
    """An action that is not going to be attempted. `detail` is written for
    the person who asked; `status_code` is the HTTP status it is answered with."""

    status_code = 400

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class NotFound(Refused):
    status_code = 404


class Busy(Refused):
    """Something already running would be undermined by this."""
    status_code = 409


class UpstreamFailed(Refused):
    """Portainer could not be asked what the action needed to know first. A
    guard that cannot look does not pass."""
    status_code = 502


def describe(exc: BaseException) -> str:
    """What went wrong, in words someone can act on.

    Portainer's, a registry's and Restruo's own errors already are such words.
    Anything else
    is named by its type: some failures stringify to nothing — httpx timeouts
    in particular — and "ConnectError: …" says more than the text alone.
    """
    if isinstance(exc, PortainerError):
        return exc.message
    if isinstance(exc, Refused):
        return exc.detail
    text = str(exc).strip()
    if isinstance(exc, RegistryError):
        return text or f"HTTP {exc.status_code}"
    name = type(exc).__name__
    return f"{name}: {text}" if text else name
