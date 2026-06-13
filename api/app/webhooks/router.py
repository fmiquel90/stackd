from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt
from app.db import get_session
from app.enums import RunType, TriggeredBy
from app.errors import ProblemException
from app.logging import get_logger
from app.models.environment import Environment
from app.models.stack import Stack
from app.runs.service import trigger_run

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
_log = get_logger("stackd.webhooks")


def _normalize(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")


async def _candidate_stacks(session: AsyncSession, payload: dict) -> list[Stack]:
    repo = payload.get("repository", {})
    urls = {
        _normalize(u)
        for u in (
            repo.get("clone_url"),
            repo.get("html_url"),
            repo.get("ssh_url"),
            repo.get("git_url"),
        )
        if u
    }
    all_stacks = (await session.execute(select(Stack))).scalars().all()
    return [s for s in all_stacks if _normalize(s.repo_url) in urls and s.webhook_secret_encrypted]


def _verify(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def _touches_root(payload: dict, project_root: str) -> bool:
    if project_root in (".", ""):
        return True
    changed: list[str] = []
    for commit in payload.get("commits", []):
        for key in ("added", "modified", "removed"):
            changed.extend(commit.get(key, []))
    if not changed:
        return True  # no file info → be conservative and run
    root = project_root.rstrip("/") + "/"
    return any(path.startswith(root) for path in changed)


@router.post("/github")
async def github_webhook(
    request: Request,
    session: DbSession,
    x_github_event: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict:
    body = await request.body()
    payload = json.loads(body)
    stacks = await _candidate_stacks(session, payload)
    if not stacks:
        return {"matched": 0}

    secret = decrypt(stacks[0].webhook_secret_encrypted)  # shared per repo (§3.1)
    if not _verify(secret, body, x_hub_signature_256):
        _log.warning(
            "webhook rejected",
            extra={"event": "webhook.rejected", "reason": "bad_hmac", "stacks": len(stacks)},
        )
        raise ProblemException(
            401, "Webhook rejected", "Invalid HMAC signature. Check the secret in Settings."
        )

    triggered = 0
    if x_github_event == "push":
        branch = payload.get("ref", "").removeprefix("refs/heads/")
        after = payload.get("after")
        for stack in stacks:
            envs = (
                (
                    await session.execute(
                        select(Environment).where(
                            Environment.stack_id == stack.id, Environment.branch == branch
                        )
                    )
                )
                .scalars()
                .all()
            )
            for env in envs:
                # Staleness (§9.6): head_sha always advances, even when no run is triggered.
                env.head_sha = after
                env.head_updated_at = datetime.now(UTC)
                if _touches_root(payload, stack.project_root):
                    await trigger_run(
                        session,
                        env,
                        run_type=RunType.tracked,
                        triggered_by=TriggeredBy.webhook,
                        commit_sha=after,
                    )
                    triggered += 1
        await session.commit()

    elif x_github_event == "pull_request":
        if payload.get("action") in ("opened", "synchronize", "reopened"):
            pr = payload.get("pull_request", {})
            base_branch = pr.get("base", {}).get("ref")
            head_sha = pr.get("head", {}).get("sha")
            for stack in stacks:
                envs = (
                    (
                        await session.execute(
                            select(Environment).where(
                                Environment.stack_id == stack.id, Environment.branch == base_branch
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for env in envs:
                    # PR → proposed (plan-only) run (§5 / §13).
                    await trigger_run(
                        session,
                        env,
                        run_type=RunType.proposed,
                        triggered_by=TriggeredBy.webhook,
                        commit_sha=head_sha,
                    )
                    triggered += 1

    _log.info(
        "webhook processed",
        extra={
            "event": "webhook.received",
            "github_event": x_github_event,
            "matched": len(stacks),
            "triggered": triggered,
        },
    )
    return {"matched": len(stacks), "triggered": triggered}
