from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crypto import decrypt
from app.logging import get_logger
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.vcs import VcsOutbox
from app.vcs import github

_log = get_logger("stackd.vcs")
_MAX_ATTEMPTS = 5

# run state → GitHub commit-status state (a `proposed` run is plan-only, terminal at `finished`).
_GH_STATE = {
    "planning": "pending",
    "finished": "success",
    "failed": "failure",
    "canceled": "error",
    "discarded": "error",
}


def _run_url(run_id: object) -> str | None:
    s = get_settings()
    base = s.stackd_app_url or s.stackd_public_url
    return f"{base.rstrip('/')}/runs/{run_id}" if base else None


def _summary(run: Run) -> str:
    ps = run.plan_summary or {}
    return f"+{ps.get('add', 0)} ~{ps.get('change', 0)} -{ps.get('destroy', 0)}"


def _comment_body(run: Run, stack: Stack, env: Environment) -> str:
    lines = [f"**StackD — `{stack.name}/{env.name}` ({env.tier})**", ""]
    state = run.state.value
    if state == "finished":
        lines.append(f"Plan: `{_summary(run)}`")
    elif state == "failed":
        lines.append("Plan **failed**." + (f" `{run.error}`" if run.error else ""))
    elif state in ("canceled", "discarded"):
        lines.append(f"Run {state}.")
    else:
        lines.append("Planning…")
    if run.used_mocks:
        lines.append("\n> ⚠ used mocks — apply is blocked")
    url = _run_url(run.id)
    if url:
        lines.append(f"\n[Open run]({url})")
    return "\n".join(lines)


async def dispatch_vcs(session: AsyncSession, now: datetime, *, limit: int = 20) -> int:
    """Drain the VCS outbox (same shape as the notification dispatcher): claim a batch + bump
    attempts (locked, brief), post the commit status + upsert the PR comment with no DB txn held,
    then mark sent and persist any new comment id. At-least-once; advisory-locked to stay single."""
    rows = (
        (
            await session.execute(
                select(VcsOutbox)
                .where(VcsOutbox.sent_at.is_(None), VcsOutbox.attempts < _MAX_ATTEMPTS)
                .order_by(VcsOutbox.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    batch = [(r.id, r.run_id, r.to_state, r.attempts + 1) for r in rows]
    for r in rows:
        r.attempts += 1
    await session.commit()

    done: list[tuple] = []  # (row_id, run_id, comment_id|None)
    for row_id, run_id, to_state, attempt in batch:
        run = await session.get(Run, run_id)
        if run is None or run.vcs_provider != "github":
            done.append((row_id, None, None))
            continue
        env = await session.get(Environment, run.environment_id)
        stack = await session.get(Stack, env.stack_id) if env else None
        owner_repo = github.parse_owner_repo(stack.repo_url) if stack else None
        token = (
            decrypt(stack.repo_secret_encrypted)
            if (stack and stack.repo_secret_encrypted)
            else None
        )
        if env is None or stack is None or owner_repo is None or token is None:
            done.append((row_id, None, None))  # not postable (no creds / not github) — drop
            continue
        owner, repo = owner_repo
        try:
            if run.vcs_head_sha:
                await github.set_status(
                    token,
                    owner,
                    repo,
                    run.vcs_head_sha,
                    state=_GH_STATE.get(to_state, "pending"),
                    target_url=_run_url(run.id),
                    context="stackd/plan",
                    description=_summary(run) if to_state == "finished" else to_state,
                )
            comment_id = run.vcs_comment_id
            if run.pr_number:
                comment_id = await github.upsert_comment(
                    token,
                    owner,
                    repo,
                    run.pr_number,
                    _comment_body(run, stack, env),
                    run.vcs_comment_id,
                )
            done.append((row_id, run_id, comment_id))
        except Exception as exc:  # external API — never crash the loop
            _log.warning(
                "vcs post-back failed",
                extra={
                    "event": "vcs.failed",
                    "run_id": str(run_id),
                    "state": to_state,
                    "attempt": attempt,
                    "error": str(exc),
                },
            )

    if done:
        await session.execute(
            update(VcsOutbox).where(VcsOutbox.id.in_([d[0] for d in done])).values(sent_at=now)
        )
        for _row_id, run_id, comment_id in done:
            if run_id is not None and comment_id is not None:
                await session.execute(
                    update(Run).where(Run.id == run_id).values(vcs_comment_id=comment_id)
                )
        await session.commit()
    return len(batch)
