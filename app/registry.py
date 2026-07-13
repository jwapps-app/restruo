"""Container-registry client for update checks.

Detecting a newer "latest" is a digest comparison: the tag is a floating
pointer, so we HEAD the registry's manifest for the tag and compare its digest
against the digest of the image currently on the machine. Works anonymously
against Docker Hub, ghcr.io, lscr.io and any other v2 registry that supports
token auth.
"""

import re
from dataclasses import dataclass

import httpx

REGISTRY_TIMEOUT = 15.0

# Accept both manifest lists (multi-arch) and single manifests so the returned
# Docker-Content-Digest matches what `docker pull` records in RepoDigests.
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)

_CHALLENGE_FIELD_RE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class ImageRef:
    raw: str
    registry: str
    repository: str
    tag: str
    pinned_digest: bool

    @property
    def tracks_latest(self) -> bool:
        """True when this image floats on :latest (or has no tag at all)."""
        return self.tag == "latest" and not self.pinned_digest


def parse_image_ref(raw: str) -> ImageRef | None:
    """Parse a compose `image:` value. Returns None for values we can't
    reason about (e.g. unresolved ${VAR} interpolation)."""
    s = raw.strip()
    if not s or "$" in s:
        return None

    pinned_digest = "@" in s
    if pinned_digest:
        s = s.partition("@")[0]

    first, _, rest = s.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, name = first, rest
    else:
        registry, name = "docker.io", s

    last_segment = name.rsplit("/", 1)[-1]
    if ":" in last_segment:
        name, _, tag = name.rpartition(":")
    else:
        tag = "latest"

    if registry == "docker.io" and "/" not in name:
        name = f"library/{name}"  # official images live under library/

    if not name:
        return None
    return ImageRef(raw=raw, registry=registry, repository=name, tag=tag,
                    pinned_digest=pinned_digest)


class RegistryClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        kwargs = {} if transport is None else {"transport": transport}
        self._client = httpx.AsyncClient(timeout=REGISTRY_TIMEOUT, **kwargs)
        self._tokens: dict[tuple[str, str], str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _api_host(ref: ImageRef) -> str:
        # Docker Hub's registry API lives on a different host than its name.
        return "registry-1.docker.io" if ref.registry == "docker.io" else ref.registry

    async def get_remote_digest(self, ref: ImageRef) -> str:
        host = self._api_host(ref)
        url = f"https://{host}/v2/{ref.repository}/manifests/{ref.tag}"
        headers = {"Accept": MANIFEST_ACCEPT}

        token = self._tokens.get((host, ref.repository))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = await self._client.head(url, headers=headers)

        if response.status_code == 401:
            challenge = response.headers.get("www-authenticate", "")
            token = await self._fetch_token(challenge, ref)
            self._tokens[(host, ref.repository)] = token
            headers["Authorization"] = f"Bearer {token}"
            response = await self._client.head(url, headers=headers)

        if response.status_code != 200:
            raise RegistryError(
                f"{host} returned {response.status_code} for {ref.repository}:{ref.tag}"
            )
        digest = response.headers.get("docker-content-digest")
        if not digest:
            raise RegistryError(f"{host} returned no digest for {ref.repository}:{ref.tag}")
        return digest

    async def _fetch_token(self, challenge: str, ref: ImageRef) -> str:
        fields = dict(_CHALLENGE_FIELD_RE.findall(challenge))
        realm = fields.get("realm")
        if not challenge.lower().startswith("bearer") or not realm:
            raise RegistryError(f"unsupported auth challenge from {ref.registry}")
        params = {"scope": fields.get("scope") or f"repository:{ref.repository}:pull"}
        if fields.get("service"):
            params["service"] = fields["service"]
        response = await self._client.get(realm, params=params)
        if response.status_code != 200:
            raise RegistryError(f"token request to {realm} failed ({response.status_code})")
        body = response.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            raise RegistryError(f"no token in response from {realm}")
        return token
