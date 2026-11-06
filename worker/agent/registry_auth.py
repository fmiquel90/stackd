"""Registry credentials for pulling runner images (SPECS §7.4, `docker` runner).

`STACKD_REGISTRY_AUTH_FILE` names a Docker client config file (`~/.docker/config.json` format):

    {"auths": {"registry.example.com": {"auth": "<base64 user:password>"},
               "https://index.docker.io/v1/": {"username": "u", "password": "p"},
               "myreg.azurecr.io": {"auth": "<base64 00000000-…:>", "identitytoken": "<token>"}}}

The agent picks the entry of the image reference's registry host and sends it on the pull only, as
the Docker Engine API's `X-Registry-Auth` header (base64url JSON). Credential helpers (`credsStore`,
`credHelpers`) are not supported: write the credentials in `auths` (e.g. a file produced by
`docker login` with no helper configured, or by a job refreshing a short-lived registry token).

A credential is never logged, never put in an error message (`mask`), and never reaches a job
container: it travels only in the pull request's header to the local Docker Engine.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field

from agent.logging import get_logger
from agent.masking import Masker

log = get_logger()

DOCKER_HUB = "docker.io"
# Docker Hub's historical key in config.json, and its serveraddress for the Engine API.
DOCKER_HUB_SERVER = "https://index.docker.io/v1/"
_HUB_ALIASES = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})

_warned_helpers = False


class RegistryAuthError(ValueError):
    pass


def _normalize_host(host: str) -> str:
    host = host.lower()
    return DOCKER_HUB if host in _HUB_ALIASES else host


def config_key_host(key: str) -> str:
    """A config.json `auths` key → its registry host: `https://index.docker.io/v1/` →
    `docker.io`, `https://registry.example.com:5000/v2/` → `registry.example.com:5000`."""
    for scheme in ("https://", "http://"):
        if key.lower().startswith(scheme):
            key = key[len(scheme) :]
            break
    return _normalize_host(key.split("/", 1)[0])


def registry_host(ref: str) -> str:
    """The registry host of an image reference, as Docker resolves it: the first path component
    when it looks like a host (has a `.` or a `:`, or is `localhost`), else Docker Hub.
    `registry:5000/team/opentofu@sha256:…` → `registry:5000`; `stackd/opentofu@sha256:…` and
    `alpine` → `docker.io`."""
    name = ref.partition("@")[0]
    first, sep, _ = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return _normalize_host(first)
    return DOCKER_HUB


@dataclass(frozen=True)
class RegistryCredential:
    """One registry's credential. The secret fields stay out of `repr`."""

    serveraddress: str
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    identitytoken: str = field(default="", repr=False)

    def header(self) -> str:
        """The `X-Registry-Auth` value: base64url of the Engine API's AuthConfig JSON — an identity
        token (OAuth refresh token, e.g. Azure ACR) when the entry has one, else user + password."""
        body: dict[str, str] = (
            {"identitytoken": self.identitytoken, "serveraddress": self.serveraddress}
            if self.identitytoken
            else {
                "username": self.username,
                "password": self.password,
                "serveraddress": self.serveraddress,
            }
        )
        return base64.urlsafe_b64encode(json.dumps(body).encode()).decode()

    def mask(self, text: str) -> str:
        """`text` with every form of this credential masked: the secrets themselves, the header,
        and the config file's `auth` encodings."""
        values = [self.password, self.identitytoken]
        if self.password or self.identitytoken:
            values.append(self.header())
        if self.password:
            pair = f"{self.username}:{self.password}".encode()
            values += [base64.b64encode(pair).decode(), base64.urlsafe_b64encode(pair).decode()]
        return Masker([v for v in values if v]).mask(text)


def _entry(key: str, raw: object) -> RegistryCredential | None:
    if not isinstance(raw, dict):
        return None
    host = config_key_host(key)
    server = DOCKER_HUB_SERVER if host == DOCKER_HUB else host
    username = str(raw.get("username") or "")
    password = str(raw.get("password") or "")
    auth = raw.get("auth")
    if auth:
        try:
            decoded = base64.b64decode(str(auth), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            # Never quote the value: it is the credential.
            raise RegistryAuthError(
                f"STACKD_REGISTRY_AUTH_FILE: the `auth` of {key!r} is not base64 of user:password"
            ) from None
        user, sep, pwd = decoded.partition(":")
        if not sep:
            raise RegistryAuthError(
                f"STACKD_REGISTRY_AUTH_FILE: the `auth` of {key!r} is not base64 of user:password"
            )
        username, password = user, pwd
    token = str(raw.get("identitytoken") or "")
    if not token and not (username and password):
        return None  # e.g. `{}`, what `docker login` writes when a credential helper holds it
    return RegistryCredential(server, username, password, token)


def load_auths(path: str) -> dict[str, RegistryCredential]:
    """The credentials of a Docker config file, by registry host. Raises `RegistryAuthError` on an
    unreadable or malformed file — whose message never quotes the file's content."""
    global _warned_helpers
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except OSError as exc:
        raise RegistryAuthError(
            f"STACKD_REGISTRY_AUTH_FILE {path}: cannot read it ({exc.strerror})"
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        where = f" (line {exc.lineno})" if isinstance(exc, json.JSONDecodeError) else ""
        raise RegistryAuthError(
            f"STACKD_REGISTRY_AUTH_FILE {path}: not a JSON document{where}"
        ) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("auths", {}), dict):
        raise RegistryAuthError(f"STACKD_REGISTRY_AUTH_FILE {path}: `auths` must be an object")
    if (doc.get("credsStore") or doc.get("credHelpers")) and not _warned_helpers:
        _warned_helpers = True
        log.warning(
            "credential helpers are not supported: only `auths` entries are used",
            extra={"event": "agent.registry_auth_helpers_ignored"},
        )
    creds: dict[str, RegistryCredential] = {}
    for key, raw in doc.get("auths", {}).items():
        cred = _entry(str(key), raw)
        if cred is not None:
            creds[config_key_host(str(key))] = cred
    return creds


def credential_for(ref: str, path: str | None) -> RegistryCredential | None:
    """The credential for pulling `ref`, from the file at `path` (re-read: a rotated credential
    applies to the next pull), or None — an anonymous pull."""
    if not path:
        return None
    return load_auths(path).get(registry_host(ref))
