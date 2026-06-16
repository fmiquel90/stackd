from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
        shutil.rmtree(dest, ignore_errors=True)
        raise ProblemException(502, "Repository clone failed", str(exc)) from exc
    if proc.returncode != 0:
        msg = err.decode(errors="replace")
        if secret:  # the authed URL may appear in git's error — never leak the token
            msg = msg.replace(secret, "•••")
        shutil.rmtree(dest, ignore_errors=True)
        raise ProblemException(502, "Repository clone failed", msg.strip()[:500] or None)
    return dest


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
            env={
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
            },
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (TimeoutError, FileNotFoundError) as exc:
        return False, [], f"git unavailable or timed out: {exc}"

    if proc.returncode != 0:
        return False, [], err.decode(errors="replace").strip()[:500] or "repository unreachable"

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
            env={
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
            },
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (TimeoutError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    line = out.decode().strip()
    return line.split("\t", 1)[0] if line else None
