from __future__ import annotations

import re

import httpx

from app.config import get_settings

_HTTP_TIMEOUT = 8.0
# owner/repo from https://github.com/o/r(.git) or git@github.com:o/r(.git)
_OWNER_REPO = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


def parse_owner_repo(repo_url: str) -> tuple[str, str] | None:
    m = _OWNER_REPO.search((repo_url or "").strip())
    return (m.group("owner"), m.group("repo")) if m else None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api() -> str:
    return get_settings().stackd_github_api_url.rstrip("/")


async def set_status(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    *,
    state: str,
    target_url: str | None,
    context: str,
    description: str,
) -> None:
    """Commit Status API (PAT-compatible). `state` ∈ {pending,success,failure,error}."""
    body: dict[str, str] = {"state": state, "context": context, "description": description[:140]}
    if target_url:
        body["target_url"] = target_url
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
        resp = await http.post(
            f"{_api()}/repos/{owner}/{repo}/statuses/{sha}", headers=_headers(token), json=body
        )
        resp.raise_for_status()


async def upsert_comment(
    token: str, owner: str, repo: str, pr_number: int, body: str, comment_id: int | None
) -> int:
    """Edit the existing PR comment if `comment_id`, else create one. Returns the comment id."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
        if comment_id:
            resp = await http.patch(
                f"{_api()}/repos/{owner}/{repo}/issues/comments/{comment_id}",
                headers=_headers(token),
                json={"body": body},
            )
            resp.raise_for_status()
            return comment_id
        resp = await http.post(
            f"{_api()}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=_headers(token),
            json={"body": body},
        )
        resp.raise_for_status()
        return int(resp.json()["id"])
