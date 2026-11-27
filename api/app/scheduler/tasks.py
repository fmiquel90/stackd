from __future__ import annotations

import asyncio
import functools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.crypto import decrypt
from app.db import SessionLocal, get_engine
from app.drift.service import detect_drift
from app.enums import ACTIVE_STATES, RepoAuthKind, RunEventActor, RunState, WorkerStatus
from app.logging import get_logger
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.user import RefreshToken
from app.models.worker import Worker
from app.notifications.dispatcher import dispatch_pending
from app.observability.metrics import scheduler_task_failures
from app.oidc.issuer import retire_keys
from app.rollouts.service import expire_approvals
from app.runs.logs_archive import archive_run_logs, purge_hot_logs
from app.runs.transition import transition
from app.stacks.git import ls_remote_sha
from app.vcs.dispatcher import dispatch_vcs
from app.webhooks.replay import purge_webhook_deliveries
from app.workers.claim import holds_apply_credentials

# Distinct advisory-lock keys so each periodic task runs once across replicas (§7.5).
_LOCK_WORKER_LOST = 74001
_LOCK_NOTIFY = 74002
_LOCK_VCS = 74003
_LOCK_DRIFT = 74004
_LOCK_HEAD_POLL = 74005
_LOCK_OIDC_KEYS = 74006
# 74007 is the OIDC issuer's key-management lock (oidc/issuer.py, §3.11): never reuse it here.
_LOCK_ROLLOUTS = 74008
_LOCK_LOG_ARCHIVE = 74009
_LOCK_RETENTION = 74010
_RETENTION_PERIOD = timedelta(days=1)
_TICK_SECONDS = 10  # the scheduler loop's period
_HEAD_POLL_CONCURRENCY = 8  # `git ls-remote` calls a head poll runs at once (§9.6)
_OUTBOX_BATCH = 20  # rows an outbox drain claims at once
# Time budget of one run of each task (seconds): past it the run is cancelled and logged, and the
# task runs again on its next turn. The head poll's budget is its interval, the retention's a day.
_TASK_BUDGETS = {
    "worker_lost": 30.0,
    "drift": 60.0,
    "oidc_keys": 30.0,
    "rollouts": 60.0,
    "log_archive": 120.0,  # S3 uploads, up to 20 runs
    # The dispatchers bound each delivery themselves (30 s, a few in parallel): this is a backstop.
    "notifications": 300.0,
    "vcs": 300.0,
}
_AUDIT_PURGE_BATCH = 5000  # audit rows deleted per transaction (§6.1)
_log = get_logger("stackd.scheduler")

# Active states in which a worker is executing the run's job — the only ones worker-lost reclaims
# (§3.7). `unconfirmed` waits on a human and `confirmed` on the apply claim: the plan job is over
# (its workspace is wiped, nothing is left on the worker) and the apply re-clones the run's commit
# on whichever worker claims it — any worker after the affinity window (§7.2). Workers are
# disposable, so the plan worker going away must not fail them.
_AWAITING_STATES = frozenset({RunState.unconfirmed, RunState.confirmed})
_WORKER_EXECUTING_STATES = ACTIVE_STATES - _AWAITING_STATES


async def detect_worker_lost(session: AsyncSession, now: datetime) -> int:
    """Mark stale workers offline; fail runs whose job was executing on a dead worker (§3.7,
    §7.5). Runs awaiting a human or the apply claim don't depend on their plan worker."""
    settings = get_settings()
    offline_cutoff = now - timedelta(seconds=settings.stackd_worker_offline_seconds)
    await session.execute(
        update(Worker)
        .where(Worker.last_heartbeat_at < offline_cutoff, Worker.status != WorkerStatus.offline)
        .values(status=WorkerStatus.offline)
    )
    await session.flush()

    offline_ids = (
        (await session.execute(select(Worker.id).where(Worker.status == WorkerStatus.offline)))
        .scalars()
        .all()
    )
    if not offline_ids:
        await session.commit()
        return 0

    lost_cutoff = now - timedelta(seconds=settings.stackd_worker_lost_seconds)
    # A job holding apply credentials — an applying run, or a mutating command run (§4.3) — is
    # not reclaimed before the apply budget + grace (§4.2): failing a healthy long apply mid-flight
    # would let a second run claim the env and write state concurrently (split-brain). Its state and
    # OIDC tokens are minted for exactly this window (same `holds_apply_credentials` definition as
    # the claim), so by the time this cutoff passes they have expired and a reclaimed job can no
    # longer write state.
    apply_cutoff = now - timedelta(seconds=settings.apply_reclaim_window_seconds)
    candidates = (
        (
            await session.execute(
                select(Run).where(
                    Run.worker_id.in_(offline_ids),
                    Run.state.in_(_WORKER_EXECUTING_STATES),
                    Run.claimed_at < max(lost_cutoff, apply_cutoff),
                )
            )
        )
        .scalars()
        .all()
    )
    runs = [
        run
        for run in candidates
        if run.claimed_at is not None
        and run.claimed_at < (apply_cutoff if holds_apply_credentials(run) else lost_cutoff)
    ]
    for run in runs:
        await transition(
            session,
            run,
            RunState.failed,
            actor=RunEventActor.system,
            fields={"error": "worker_lost"},
            audit_action="run.apply_failed",
            audit_context={"reason": "worker_lost"},
        )
    await session.commit()
    if runs:
        _log.warning(
            "worker_lost runs failed",
            extra={"event": "scheduler.worker_lost", "count": len(runs)},
        )
    return len(runs)


async def poll_branch_heads(session: AsyncSession, now: datetime) -> int:
    """Fallback git staleness polling (§7.5, §9.6): one `git ls-remote` per (repo, branch) —
    deduplicated across the stacks sharing a repo, keyed on their credentials too so no stack's
    head is read with another stack's token — then record that head on every env tracking it.
    Returns the number of envs updated.

    Only envs whose head wasn't observed within the interval are polled (by a push webhook, a
    manual refresh or a previous poll — on any replica), so a re-run is a no-op. No DB transaction
    is held across the git calls, which run concurrently — at most `_HEAD_POLL_CONCURRENCY` at a
    time, so many unreachable repos (15 s timeout each) don't serialize into a very long poll. A
    repo that fails is logged (never its token) and skipped: its envs keep their head and are
    retried at the next poll."""
    interval = get_settings().stackd_head_poll_interval
    # One tick of slack: the loop fires this on the first tick past the interval, which must find
    # the envs refreshed by the previous poll (stamped with that poll's `now`) due again.
    due = or_(
        Environment.head_updated_at.is_(None),
        Environment.head_updated_at <= now - timedelta(seconds=interval - _TICK_SECONDS),
    )
    rows = (
        await session.execute(
            select(
                Environment.id,
                Environment.branch,
                Stack.id,
                Stack.repo_url,
                Stack.repo_auth_kind,
                Stack.repo_secret_encrypted,
            )
            .join(Stack, Stack.id == Environment.stack_id)
            .where(due)
        )
    ).all()
    await session.commit()  # release the read transaction before any git call

    groups: dict[tuple[str, RepoAuthKind, str | None, str], list[uuid.UUID]] = {}
    secrets: dict[uuid.UUID, str | None] = {}
    unreadable: set[uuid.UUID] = set()
    for env_id, branch, stack_id, repo_url, auth_kind, secret_encrypted in rows:
        if stack_id in unreadable:
            continue
        if stack_id not in secrets:
            try:
                secrets[stack_id] = decrypt(secret_encrypted) if secret_encrypted else None
            except Exception as exc:  # undecryptable credentials: skip this stack only
                unreadable.add(stack_id)
                _log.warning(
                    "branch head poll failed",
                    extra={
                        "event": "scheduler.head_poll_failed",
                        "stack_id": str(stack_id),
                        "error": type(exc).__name__,
                    },
                )
                continue
        groups.setdefault((repo_url, auth_kind, secrets[stack_id], branch), []).append(env_id)

    bound = asyncio.Semaphore(_HEAD_POLL_CONCURRENCY)

    async def _read_head(
        repo_url: str,
        auth_kind: RepoAuthKind,
        secret: str | None,
        branch: str,
        env_ids: list[uuid.UUID],
    ) -> tuple[str, str, list[uuid.UUID]] | None:
        error = "unreachable or no such branch"
        try:
            async with bound:
                sha = await ls_remote_sha(repo_url, auth_kind, secret, branch)
        except Exception as exc:  # one failing repo must not abort the others
            sha, error = None, type(exc).__name__  # the type only: a message may carry the URL
        if sha is None:
            _log.warning(
                "branch head poll failed",
                extra={
                    "event": "scheduler.head_poll_failed",
                    "environment_ids": [str(e) for e in env_ids],
                    "branch": branch,
                    "error": error,
                },
            )
            return None
        return sha, branch, env_ids

    read = await asyncio.gather(*(_read_head(*key, env_ids) for key, env_ids in groups.items()))
    heads = [head for head in read if head is not None]

    updated = 0
    for sha, branch, env_ids in heads:
        # Still due and on that branch: a webhook or manual refresh that landed during the git
        # calls is fresher, and an env re-pointed to another branch meanwhile isn't tracking it.
        result = await session.execute(
            update(Environment)
            .where(Environment.id.in_(env_ids), Environment.branch == branch, due)
            .values(head_sha=sha, head_updated_at=now)
            .returning(Environment.id)
        )
        updated += len(result.all())
    await session.commit()
    return updated


async def purge_refresh_tokens(session: AsyncSession, now: datetime) -> int:
    """Delete the refresh-token families whose every token has expired (§2.5, §7.5). A family with
    a live token is kept whole — its used/revoked rows are what reuse detection looks at."""
    expired = (
        select(RefreshToken.family_id)
        .group_by(RefreshToken.family_id)
        .having(func.max(RefreshToken.expires_at) < now)
    )
    result = await session.execute(delete(RefreshToken).where(RefreshToken.family_id.in_(expired)))
    await session.commit()
    return result.rowcount or 0  # type: ignore[attr-defined]


async def purge_audit_events(now: datetime) -> int:
    """Delete the audit events older than `audit_retention_days` (§6.1). Runs on a connection of
    the distinct `stackd_audit_purge` role — the application role cannot delete, and the
    append-only trigger refuses everyone else — and records an `audit.purged` event in the same
    transaction. Deletes in batches of `_AUDIT_PURGE_BATCH`, one transaction (and one
    `audit.purged` event) each, so a large backlog never becomes one huge, long-locking delete.
    Returns the number deleted; 0 (and nothing recorded) when the purge role's URL is not
    configured."""
    settings = get_settings()
    url = settings.stackd_audit_purge_database_url
    if not url:
        return 0
    cutoff = now - timedelta(days=settings.audit_retention_days)
    engine = create_async_engine(url)
    total = 0
    try:
        while True:
            async with engine.begin() as conn:
                deleted: int = (
                    await conn.execute(
                        text(
                            "WITH d AS (DELETE FROM audit_events WHERE id IN (SELECT id FROM"
                            " audit_events WHERE created_at < :cutoff ORDER BY created_at"
                            " LIMIT :n) RETURNING 1) SELECT count(*) FROM d"
                        ).bindparams(cutoff=cutoff, n=_AUDIT_PURGE_BATCH)
                    )
                ).scalar_one()
                if deleted:
                    await conn.execute(
                        text(
                            "INSERT INTO audit_events (actor_kind, action, target_kind, context)"
                            " VALUES ('system', 'audit.purged', 'audit',"
                            " jsonb_build_object('deleted', CAST(:n AS int),"
                            " 'before', CAST(:c AS text), 'retention_days', CAST(:d AS int)))"
                        ).bindparams(
                            n=deleted, c=cutoff.isoformat(), d=settings.audit_retention_days
                        )
                    )
            total += deleted
            if deleted < _AUDIT_PURGE_BATCH:
                break
    finally:
        await engine.dispose()
    return total


async def purge_retention(session: AsyncSession, now: datetime) -> None:
    """The daily retention task (§7.5): hot log rows past their window, expired refresh-token
    families, webhook delivery ids past the replay window (§13), audit events past retention. Each
    step is independent and idempotent."""
    for step in (purge_hot_logs, purge_refresh_tokens, purge_webhook_deliveries):
        try:
            count = await step(session, now)
        except Exception as exc:
            await session.rollback()
            _log.warning(
                "retention step failed",
                extra={
                    "event": "scheduler.retention_failed",
                    "step": step.__name__,
                    "error": str(exc),
                },
            )
            continue
        if count:
            _log.info(
                "retention purge",
                extra={"event": "scheduler.retention", "step": step.__name__, "count": count},
            )
    try:
        if count := await purge_audit_events(now):
            _log.info(
                "retention purge",
                extra={
                    "event": "scheduler.retention",
                    "step": "purge_audit_events",
                    "count": count,
                },
            )
    except Exception as exc:
        _log.warning(
            "audit purge failed",
            extra={
                "event": "scheduler.retention_failed",
                "step": "purge_audit_events",
                "error": str(exc),
            },
        )


async def _with_lock(session: AsyncSession, key: int, coro) -> Any:  # type: ignore[no-untyped-def]
    """Run `coro(session, now)` only if no other replica holds `key` (§7.5); returns its result, or
    None when the lock is taken. The session-level advisory lock lives on a dedicated connection
    held for the task's whole run, never on the task's session: the tasks commit mid-way, which
    hands the session's connection back to the pool, so an unlock through the session could land on
    another pooled connection, miss, and leak the lock (later ticks would then silently skip the
    task). AUTOCOMMIT keeps the lock connection idle rather than idle-in-transaction, and the unlock
    runs even if the task's session failed."""
    async with get_engine().execution_options(isolation_level="AUTOCOMMIT").connect() as lock_conn:
        got = (
            await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)").bindparams(k=key))
        ).scalar()
        if not got:
            return None
        try:
            return await coro(session, datetime.now(UTC))
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=key))


async def _run_task(name: str, key: int, coro, budget: float) -> Any:  # type: ignore[no-untyped-def]
    """One run of a periodic task: its own session, its advisory lock and a time budget. A task
    that raises or overruns is logged and counted, and only that run is lost — never the other
    tasks (§7.5). Returns the task's result, None if it failed or another replica holds the lock."""
    try:
        async with asyncio.timeout(budget):
            async with SessionLocal() as session:
                return await _with_lock(session, key, coro)
    except TimeoutError:
        scheduler_task_failures.labels(task=name).inc()
        _log.error(
            "scheduler task over its time budget",
            extra={"event": "scheduler.task_failed", "task": name, "error": "timeout"},
        )
    except Exception:
        scheduler_task_failures.labels(task=name).inc()
        _log.exception(
            "scheduler task failed", extra={"event": "scheduler.task_failed", "task": name}
        )
    return None


async def _tick() -> None:
    """The every-tick tasks, one after the other — each isolated by `_run_task`."""
    for name, key, coro in (
        ("worker_lost", _LOCK_WORKER_LOST, detect_worker_lost),
        ("drift", _LOCK_DRIFT, detect_drift),
        ("oidc_keys", _LOCK_OIDC_KEYS, retire_keys),
        ("rollouts", _LOCK_ROLLOUTS, expire_approvals),
        ("log_archive", _LOCK_LOG_ARCHIVE, archive_run_logs),
    ):
        await _run_task(name, key, coro, _TASK_BUDGETS[name])


async def _outbox_loop(name: str, key: int, drain) -> None:  # type: ignore[no-untyped-def]
    """An outbound dispatcher in a loop of its own, so its HTTP calls never delay the tick's tasks
    (§7.5). A full batch is followed at once by the next one; otherwise the loop waits a tick."""
    while True:
        processed = await _run_task(
            name, key, functools.partial(drain, limit=_OUTBOX_BATCH), _TASK_BUDGETS[name]
        )
        if processed != _OUTBOX_BATCH:
            await asyncio.sleep(_TICK_SECONDS)


async def scheduler_loop() -> None:
    """The periodic tasks (§7.5), each advisory-locked + idempotent, isolated from one another
    (`_run_task`). The cheap ones run every 10 s tick, in turn. Those making outbound or long calls
    run in tasks of their own so they never delay a tick (worker_lost detection above all): the
    two outbox dispatchers loop on their own, and the git staleness poll (§9.6) and the daily
    retention purge are started when due — one still running when the next is due skips that one.
    Cancelling the loop cancels and awaits them all."""
    next_head_poll: datetime | None = None
    next_retention: datetime | None = None
    head_poll: asyncio.Task[Any] | None = None
    retention: asyncio.Task[Any] | None = None
    outboxes = [
        asyncio.create_task(_outbox_loop("notifications", _LOCK_NOTIFY, dispatch_pending)),
        asyncio.create_task(_outbox_loop("vcs", _LOCK_VCS, dispatch_vcs)),
    ]
    try:
        while True:
            await _tick()
            # Each is scheduled before it runs, so a failing one also waits a full period.
            now = datetime.now(UTC)
            if next_retention is None or now >= next_retention:
                next_retention = now + _RETENTION_PERIOD
                if retention is None or retention.done():
                    retention = asyncio.create_task(
                        _run_task(
                            "retention",
                            _LOCK_RETENTION,
                            purge_retention,
                            _RETENTION_PERIOD.total_seconds(),
                        )
                    )
            if next_head_poll is None or now >= next_head_poll:
                interval = get_settings().stackd_head_poll_interval
                next_head_poll = now + timedelta(seconds=interval)
                if head_poll is None or head_poll.done():
                    head_poll = asyncio.create_task(
                        _run_task("head_poll", _LOCK_HEAD_POLL, poll_branch_heads, interval)
                    )
            await asyncio.sleep(_TICK_SECONDS)
    finally:
        children = [t for t in (*outboxes, head_poll, retention) if t is not None]
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
