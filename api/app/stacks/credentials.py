"""The stack's repository credential (SPECS §3.1): validation at write time, and the values the
worker masks once the claim hands it over (§7.2)."""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import quote

from app.enums import RepoAuthKind
from app.errors import ProblemException

_PEM = re.compile(
    r"-----BEGIN ((?:[A-Z0-9]+ )*)PRIVATE KEY-----\n(.+?)\n-----END \1PRIVATE KEY-----", re.S
)
# Kinds ssh reads without a passphrase: OpenSSH's own format, and the PEM / PKCS#8 ones.
_KEY_KINDS = frozenset({"OPENSSH", "RSA", "EC", ""})
_OPENSSH_MAGIC = b"openssh-key-v1\0"
# A scp-like SSH remote: `[user@]host:path`, with no scheme.
_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:(?!//)")
# Base64 lines of a key shorter than this are not masked on their own: a short tail line
# ("AA==") would mask unrelated log text. The whole key is masked as well.
_MIN_MASKED_LINE = 16


def is_ssh_url(repo_url: str) -> bool:
    return repo_url.startswith("ssh://") or bool(_SCP_LIKE.match(repo_url))


def _problem(detail: str) -> ProblemException:
    return ProblemException(422, "Invalid repository credential", detail)


def _openssh_cipher(body: str) -> bytes:
    """The cipher name of an `openssh-key-v1` blob: `none` for a key without a passphrase."""
    try:
        raw = base64.b64decode("".join(body.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _problem("The deploy key's body is not valid base64.") from exc
    if not raw.startswith(_OPENSSH_MAGIC) or len(raw) < len(_OPENSSH_MAGIC) + 4:
        raise _problem("The deploy key is not an OpenSSH private key.")
    at = len(_OPENSSH_MAGIC)
    size = int.from_bytes(raw[at : at + 4], "big")
    return raw[at + 4 : at + 4 + size]


def normalize_deploy_key(text: str) -> str:
    """The deploy key as ssh reads it: LF line endings, a trailing newline (ssh refuses a key
    without one). 422 for anything but an SSH private key without a passphrase — the agent runs
    non-interactively, so a passphrase can never be typed. Never echoes the value."""
    key = text.replace("\r\n", "\n").strip()
    match = _PEM.fullmatch(key)
    if match is None:
        hint = (
            " That is a public key: it goes to the Git host; paste the private key here."
            if key.startswith(("ssh-", "ecdsa-", "sk-"))
            else ""
        )
        raise _problem(
            "Expected an SSH private key (-----BEGIN OPENSSH PRIVATE KEY----- …)." + hint
        )
    kind, body = match.group(1).strip(), match.group(2)
    if kind == "ENCRYPTED" or "Proc-Type: 4,ENCRYPTED" in body:
        raise _problem("The deploy key has a passphrase: use a key without one.")
    if kind not in _KEY_KINDS:
        raise _problem(f"Unsupported private key type ({kind}); use an ed25519 or RSA key.")
    if kind == "OPENSSH" and _openssh_cipher(body) != b"none":
        raise _problem("The deploy key has a passphrase: use a key without one.")
    return key + "\n"


def prepare_repo_secret(kind: RepoAuthKind, secret: str) -> str:
    """The credential to encrypt for a stack of `kind` (§3.1), or 422."""
    if kind == RepoAuthKind.deploy_key:
        return normalize_deploy_key(secret)
    if kind == RepoAuthKind.token and "PRIVATE KEY-----" in secret:
        raise _problem("This is an SSH private key: set the repository auth to deploy_key.")
    return secret


def check_repo_auth(kind: RepoAuthKind, repo_url: str) -> None:
    """A deploy key authenticates over SSH only: refuse it on any other repository URL."""
    if kind == RepoAuthKind.deploy_key and not is_ssh_url(repo_url):
        raise _problem(
            "A deploy key authenticates over SSH: use an ssh:// or git@host:path repository URL."
        )


def credential_mask_values(kind: RepoAuthKind, secret: str) -> list[str]:
    """What the agent masks for the stack's credential (§5.1): a token raw and percent-encoded
    (the form it takes inside a URL, where git can echo it); a deploy key whole and line by line
    (log lines are masked one at a time)."""
    if kind == RepoAuthKind.deploy_key:
        lines = [
            line.strip()
            for line in secret.splitlines()
            if len(line.strip()) >= _MIN_MASKED_LINE and not line.startswith("-----")
        ]
        return [secret.strip(), *lines]
    return list(dict.fromkeys([secret, quote(secret, safe="")]))
