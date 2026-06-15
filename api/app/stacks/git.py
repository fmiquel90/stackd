from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

from app.enums import RepoAuthKind


def _authed_url(repo_url: str, auth_kind: RepoAuthKind, secret: str | None) -> str:
    if auth_kind == RepoAuthKind.token and secret and repo_url.startswith("http"):
        parts = urlsplit(repo_url)
        netloc = f"x-access-token:{secret}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return repo_url


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
