from __future__ import annotations

import asyncio
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from app.enums import RepoAuthKind
from app.errors import ProblemException

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
}


def _authed_url(repo_url: str, auth_kind: RepoAuthKind, secret: str | None) -> str:
    if auth_kind == RepoAuthKind.token and secret and repo_url.startswith("http"):
        parts = urlsplit(repo_url)
        netloc = f"x-access-token:{secret}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return repo_url


def _redact(text: str, secret: str | None) -> str:
    """Mask the repo token in anything git (or the subprocess layer) reports: the authed URL can
    appear verbatim in git's errors, and the token is write-only (invariant 3). Every message this
    module surfaces goes through here."""
    if secret:
        for form in (secret, quote(secret, safe="")):
            text = text.replace(form, "•••")
    return text


def _git_error(err: bytes, secret: str | None) -> str | None:
    # Mask BEFORE truncating, so a cut can never leave a partial token behind.
    return _redact(err.decode(errors="replace"), secret).strip()[:500] or None


async def clone_shallow(
    repo_url: str, auth_kind: RepoAuthKind, secret: str | None, branch: str
) -> Path:
    """Shallow single-branch clone into a fresh temp dir for read-only config introspection (input
    discovery, §3.4) — never runs terraform. The caller MUST clean up the returned path."""
    url = _authed_url(repo_url, auth_kind, secret)
    dest = Path(tempfile.mkdtemp(prefix="stackd-discover-"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            branch,
            url,
            str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_GIT_ENV,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (TimeoutError, FileNotFoundError) as exc:
        await asyncio.to_thread(shutil.rmtree, dest, ignore_errors=True)
        raise ProblemException(502, "Repository clone failed", _redact(str(exc), secret)) from exc
    if proc.returncode != 0:
        await asyncio.to_thread(shutil.rmtree, dest, ignore_errors=True)
        raise ProblemException(502, "Repository clone failed", _git_error(err, secret))
    return dest


def enforce_clone_budget(dest: Path, *, max_mb: int) -> None:
    """Reject a clone whose on-disk size exceeds the discovery budget (§H). The clone is already
    `--depth 1 --single-branch` (and 30s-bounded), so this only catches a single oversized commit;
    the caller cleans up `dest`."""
    total = 0
    cap = max_mb * 1024 * 1024
    # lstat + no symlink recursion: a symlink (file or dir) is never followed out of the clone.
    for f in dest.rglob("*", recurse_symlinks=False):
        st = f.lstat()
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
            if total > cap:
                raise ProblemException(
                    413,
                    "Repository too large",
                    f"Discovery clone exceeds the {max_mb} MB budget.",
                )


async def check_repo(
    repo_url: str, auth_kind: RepoAuthKind, secret: str | None
) -> tuple[bool, list[str], str | None]:
    """`git ls-remote --heads`: validate reachability + list branches (deploy keys post-MVP)."""
    url = _authed_url(repo_url, auth_kind, secret)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            "--heads",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_GIT_ENV,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (TimeoutError, FileNotFoundError) as exc:
        return False, [], _redact(f"git unavailable or timed out: {exc}", secret)

    if proc.returncode != 0:
        return False, [], _git_error(err, secret) or "repository unreachable"

    branches = [
        line.split("refs/heads/", 1)[1]
        for line in out.decode().splitlines()
        if "refs/heads/" in line
    ]
    return True, branches, None


async def ls_remote_sha(
    repo_url: str, auth_kind: RepoAuthKind, secret: str | None, branch: str
) -> str | None:
    """HEAD sha of a single branch (staleness polling / manual refresh, §9.6)."""
    url = _authed_url(repo_url, auth_kind, secret)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            url,
            f"refs/heads/{branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_GIT_ENV,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (TimeoutError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    line = out.decode().strip()
    return line.split("\t", 1)[0] if line else None
