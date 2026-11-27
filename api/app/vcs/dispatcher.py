from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import outbox
from app.config import get_settings
from app.crypto import decrypt
from app.enums import RepoAuthKind
from app.logging import get_logger
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.vcs import VcsOutbox
from app.notifications.dispatcher import run_url
from app.observability.metrics import outbox_dead_letters
from app.vcs import github

_log = get_logger("stackd.vcs")
_POST_CONCURRENCY = 4  # stacks posted back at once (one stack's rows go one at a time)
_POST_BUDGET = 30.0  # one row's post-back: the commit status, then the PR comment

# run state → GitHub commit-status state (a `proposed` run is plan-only, terminal at `finished`).
_GH_STATE = {
    "planning": "pending",
    "finished": "success",
    "failed": "failure",
    "canceled": "error",
    "discarded": "error",
}


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
    url = run_url(run.id)
    if url:
        lines.append(f"\n[Open run]({url})")
    return "\n".join(lines)


@dataclass
class _Row:
    id: uuid.UUID
    run_id: uuid.UUID
    to_state: str
    attempt: int
    superseded: bool
    done: bool = False
    retry_after: float | None = None


async def dispatch_vcs(session: AsyncSession, now: datetime, *, limit: int = 20) -> int:
    """Drain the due rows of the VCS outbox (same shape as the notification dispatcher, §7.5):
    claim a batch + bump attempts + push `next_attempt_at` by the backoff (locked, brief), post the
    commit status + upsert the PR comment with no DB txn held, then mark sent and persist any new
    comment id. One stack's rows are posted in order, one at a time (GitHub asks for serial
    requests per token); different stacks' at most `_POST_CONCURRENCY` at once. At-least-once;
    advisory-locked to stay single.

    A retried row is superseded once a newer row exists for its run: that one carries a later
    state, and posting the stale status after it would roll the commit status back (a `pending`
    retry landing after the `success`)."""
    max_attempts = get_settings().stackd_outbox_max_attempts
    claimed = (
        (
            await session.execute(
                select(VcsOutbox)
                .where(
                    VcsOutbox.sent_at.is_(None),
                    VcsOutbox.attempts < max_attempts,
                    VcsOutbox.next_attempt_at <= now,
                )
                .order_by(VcsOutbox.next_attempt_at, VcsOutbox.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not claimed:
        return 0

    latest: dict[uuid.UUID, datetime] = {
        run_id: created_at
        for run_id, created_at in (
            await session.execute(
                select(VcsOutbox.run_id, func.max(VcsOutbox.created_at))
                .where(VcsOutbox.run_id.in_({r.run_id for r in claimed}))
                .group_by(VcsOutbox.run_id)
            )
        ).all()
    }
    rows: list[_Row] = []
    for r in claimed:
        superseded = r.attempts > 0 and r.created_at < latest[r.run_id]
        r.attempts += 1
        r.next_attempt_at = outbox.next_attempt_at(now, r.attempts)
        rows.append(_Row(r.id, r.run_id, r.to_state, r.attempts, superseded))
    await session.commit()

    # One joined fetch for the whole batch (instead of 3 point queries per row); a run missing its
    # env/stack simply doesn't join and is dropped below.
    triples = (
        await session.execute(
            select(Run, Environment, Stack)
            .join(Environment, Environment.id == Run.environment_id)
            .join(Stack, Stack.id == Environment.stack_id)
            .where(Run.id.in_({r.run_id for r in rows}))
        )
    ).all()
    by_run = {run.id: (run, env, stack) for run, env, stack in triples}
    # End the read txn before any HTTP so no pooled connection idles in transaction across the
    # batch. Commit, not rollback (which would expire the rows): with expire_on_commit=False the
    # loaded rows stay usable — plain columns only, no relationship to lazy-load.
    await session.commit()

    # Latest PR comment id per run, refreshed after every upsert: a batch can hold several rows for
    # one run (a fast plan, a backlog) and each must edit that one comment, not post a new one.
    comment_ids: dict[uuid.UUID, int] = {}

    async def _post(row: _Row, http: httpx.AsyncClient) -> None:
        if row.superseded or row.run_id not in by_run:
            row.done = True
            return
        run, env, stack = by_run[row.run_id]
        if run.vcs_provider != "github":
            row.done = True
            return
        try:
            async with asyncio.timeout(_POST_BUDGET):
                # Inside the try: one stack's undecryptable secret (key rotation, corrupt blob)
                # fails that row only — retried / dead-lettered — never the rest of the batch.
                owner_repo = github.parse_owner_repo(stack.repo_url)
                # A deploy key (§3.1) is no PAT: it is never sent to the VCS API.
                token = (
                    decrypt(stack.repo_secret_encrypted)
                    if stack.repo_secret_encrypted
                    and stack.repo_auth_kind != RepoAuthKind.deploy_key
                    else None
                )
                if owner_repo is None or token is None:
                    row.done = True  # not postable (no creds / not github) — drop
                    return
                owner, repo = owner_repo
                if run.vcs_head_sha:
                    await github.set_status(
                        token,
                        owner,
                        repo,
                        run.vcs_head_sha,
                        state=_GH_STATE.get(row.to_state, "pending"),
                        target_url=run_url(run.id),
                        context="stackd/plan",
                        description=_summary(run) if row.to_state == "finished" else row.to_state,
                        http=http,
                    )
                if run.pr_number:
                    comment_ids[row.run_id] = await github.upsert_comment(
                        token,
                        owner,
                        repo,
                        run.pr_number,
                        _comment_body(run, stack, env),
                        comment_ids.get(row.run_id, run.vcs_comment_id),
                        http=http,
                    )
                row.done = True
        except Exception as exc:  # external API — never crash the drain
            row.retry_after = outbox.retry_after_seconds(exc, datetime.now(UTC))
            _log.warning(
                "vcs post-back failed",
                extra={
                    "event": "vcs.failed",
                    "run_id": str(row.run_id),
                    "state": row.to_state,
                    "attempt": row.attempt,
                    "error": str(exc) or type(exc).__name__,  # InvalidTag has no message
                },
            )

    by_stack: dict[uuid.UUID | None, list[_Row]] = {}
    for row in rows:
        stack_id = by_run[row.run_id][2].id if row.run_id in by_run else None
        by_stack.setdefault(stack_id, []).append(row)
    bound = asyncio.Semaphore(_POST_CONCURRENCY)

    async def _post_stack(stack_rows: list[_Row], http: httpx.AsyncClient) -> None:
        async with bound:
            for row in stack_rows:
                await _post(row, http)

    # One pooled client for the whole batch — post-backs reuse its pooled TLS connections.
    async with github.client() as http:
        await asyncio.gather(*(_post_stack(stack_rows, http) for stack_rows in by_stack.values()))

    for row in rows:
        if row.done:
            continue
        if row.attempt >= max_attempts:
            outbox_dead_letters.labels(outbox="vcs").inc()
            _log.error(
                "vcs post-back abandoned after its last attempt",
                extra={
                    "event": "vcs.dead_lettered",
                    "outbox_id": str(row.id),
                    "run_id": str(row.run_id),
                    "state": row.to_state,
                    "attempts": row.attempt,
                },
            )
        elif row.retry_after is not None:
            await session.execute(
                update(VcsOutbox)
                .where(VcsOutbox.id == row.id)
                .values(
                    next_attempt_at=outbox.next_attempt_at(
                        datetime.now(UTC), row.attempt, row.retry_after
                    )
                )
            )
    done = [row.id for row in rows if row.done]
    if done:
        await session.execute(update(VcsOutbox).where(VcsOutbox.id.in_(done)).values(sent_at=now))
    for run_id, comment_id in comment_ids.items():
        await session.execute(update(Run).where(Run.id == run_id).values(vcs_comment_id=comment_id))
    await session.commit()
    return len(rows)
