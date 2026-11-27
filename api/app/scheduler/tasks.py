from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.crypto import decrypt
from app.db import SessionLocal, get_engine
from app.drift.service import detect_drift
from app.enums import ACTIVE_STATES, RepoAuthKind, RunState, WorkerStatus
from app.logging import get_logger
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.user import RefreshToken
from app.models.worker import Worker
from app.notifications.dispatcher import dispatch_pending
from app.oidc.issuer import retire_keys
from app.rollouts.service import expire_approvals
from app.runs.logs_archive import archive_run_logs, purge_hot_logs
from app.stacks.git import ls_remote_sha
from app.vcs.dispatcher import dispatch_vcs
from app.webhooks.replay import purge_webhook_deliveries
from app.workers.reaping import UNLISTED_HEARTBEATS_TO_REAP, fail_lost_job, reclaimable

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
_log = get_logger("stackd.scheduler")

# Active states in which a worker is executing the run's job — the only ones worker-lost reclaims
# (§3.7). `unconfirmed` waits on a human and `confirmed` on the apply claim: the plan job is over
# (its workspace is wiped, nothing is left on the worker) and the apply re-clones the run's commit
# on whichever worker claims it — any worker after the affinity window (§7.2). Workers are
# disposable, so the plan worker going away must not fail them.
_AWAITING_STATES = frozenset({RunState.unconfirmed, RunState.confirmed})
_WORKER_EXECUTING_STATES = ACTIVE_STATES - _AWAITING_STATES


class _ApiClock:
    """When this process's reaper last reached the database, and the last stretch it could not
    (§7.5). Heartbeats cannot reach a stopped or cut-off API either, so the silence of a worker
    during such a stretch says nothing about the worker: it is not counted. Before the first run
    in a process, nothing is known — the whole past is such a stretch, which gives every worker one
    offline period after an API (re)start to heartbeat again before it is marked offline."""

    def __init__(self) -> None:
        self.last_seen: datetime | None = None
        self.gap: tuple[datetime | None, datetime] | None = None

    def observe(self, now: datetime) -> None:
        if self.last_seen is None:
            self.gap = (None, now)
        elif now - self.last_seen > _OUTAGE_GAP:
            self.gap = (self.last_seen, now)
        self.last_seen = now

    def silence(self, last_heartbeat_at: datetime, now: datetime) -> timedelta:
        """How long the worker has been silent while the API could hear it."""
        silent = now - last_heartbeat_at
        if self.gap is None:
            return silent
        start, end = self.gap
        hidden = end - (last_heartbeat_at if start is None else max(last_heartbeat_at, start))
        return silent - max(hidden, timedelta(0))


# A reaper run this much later than the previous one means the ticks were failing (DB or API
# outage). Only that last stretch is discounted, so slow ticks never postpone reaping for good.
_OUTAGE_GAP = timedelta(seconds=2 * _TICK_SECONDS)
_api_clock = _ApiClock()


async def detect_worker_lost(session: AsyncSession, now: datetime) -> int:
    """Mark stale workers offline; fail runs whose job no worker executes any more (§3.7, §7.5):
    on an offline worker (`worker_lost`), or left unlisted by its live worker's heartbeats
    (`job_not_running`). Runs awaiting a human or the apply claim don't depend on their plan
    worker. Each run is failed in a transaction of its own: one that cannot be (changed
    underneath) is logged and skipped, never holding back the others."""
    settings = get_settings()
    offline_after = timedelta(seconds=settings.stackd_worker_offline_seconds)
    _api_clock.observe(now)
    beats = (
        await session.execute(
            select(Worker.id, Worker.last_heartbeat_at).where(
                Worker.status != WorkerStatus.offline, Worker.last_heartbeat_at.is_not(None)
            )
        )
    ).all()
    stale = [(wid, beat) for wid, beat in beats if _api_clock.silence(beat, now) > offline_after]
    if stale:
        # Guarded on the heartbeat read: a worker that beat since is left alone.
        await session.execute(
            update(Worker)
            .where(
                tuple_(Worker.id, Worker.last_heartbeat_at).in_(stale),
                Worker.status != WorkerStatus.offline,
            )
            .values(status=WorkerStatus.offline)
        )
    await session.commit()

    # A job holding apply credentials — an applying run, or a mutating command run (§4.3) — is
    # not reclaimed before the apply budget + grace (§4.2): failing a healthy long apply mid-flight
    # would let a second run claim the env and write state concurrently (split-brain). Its state and
    # OIDC tokens are minted for exactly this window (same `holds_apply_credentials` definition as
    # the claim), so by the time this cutoff passes they have expired and a reclaimed job can no
    # longer write state (`reclaimable`).
    lost_cutoff = now - timedelta(seconds=settings.stackd_worker_lost_seconds)
    candidates = (
        await session.execute(
            select(Run.id, Worker.status)
            .join(Worker, Worker.id == Run.worker_id)
            .where(
                Run.state.in_(_WORKER_EXECUTING_STATES),
                Run.claimed_at < lost_cutoff,
                or_(
                    Worker.status == WorkerStatus.offline,
                    Run.unlisted_heartbeats >= UNLISTED_HEARTBEATS_TO_REAP,
                ),
            )
        )
    ).all()
    await session.commit()

    failed: dict[str, int] = {}
    for run_id, worker_status in candidates:
        reason = "worker_lost" if worker_status == WorkerStatus.offline else "job_not_running"
        try:
            run = await session.get(Run, run_id, populate_existing=True)
            if (
                run is None
                or run.state not in _WORKER_EXECUTING_STATES
                or not reclaimable(run, now)
            ):
                continue
            await fail_lost_job(session, run, reason)
            await session.commit()
        except Exception as exc:  # one run must not hold back the others, tick after tick
            await session.rollback()
            _log.warning(
                "worker_lost run not failed",
                extra={
                    "event": "scheduler.worker_lost_skipped",
                    "run_id": str(run_id),
                    "reason": reason,
                    "error": str(exc) or type(exc).__name__,
                },
            )
            continue
        failed[reason] = failed.get(reason, 0) + 1
    if failed:
        _log.warning(
            "worker_lost runs failed",
            extra={"event": "scheduler.worker_lost", "count": sum(failed.values()), **failed},
        )
    return sum(failed.values())


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
    transaction. Returns the number deleted; 0 (and nothing recorded) when the purge role's URL is
    not configured."""
    settings = get_settings()
    url = settings.stackd_audit_purge_database_url
    if not url:
        return 0
    cutoff = now - timedelta(days=settings.audit_retention_days)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            deleted = (
                await conn.execute(
                    text(
                        "WITH d AS (DELETE FROM audit_events WHERE created_at < :cutoff"
                        " RETURNING 1) SELECT count(*) FROM d"
                    ).bindparams(cutoff=cutoff)
                )
            ).scalar_one()
            if deleted:
                await conn.execute(
                    text(
                        "INSERT INTO audit_events (actor_kind, action, target_kind, context)"
                        " VALUES ('system', 'audit.purged', 'audit',"
                        " jsonb_build_object('deleted', CAST(:n AS int),"
                        " 'before', CAST(:c AS text), 'retention_days', CAST(:d AS int)))"
                    ).bindparams(n=deleted, c=cutoff.isoformat(), d=settings.audit_retention_days)
                )
    finally:
        await engine.dispose()
    return int(deleted)


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


async def _with_lock(session: AsyncSession, key: int, coro) -> None:  # type: ignore[no-untyped-def]
    """Run `coro(session, now)` only if no other replica holds `key` (§7.5). The session-level
    advisory lock lives on a dedicated connection held for the task's whole run, never on the
    task's session: the tasks commit mid-way, which hands the session's connection back to the
    pool, so an unlock through the session could land on another pooled connection, miss, and leak
    the lock (later ticks would then silently skip the task). AUTOCOMMIT keeps the lock connection
    idle rather than idle-in-transaction, and the unlock runs even if the task's session failed."""
    async with get_engine().execution_options(isolation_level="AUTOCOMMIT").connect() as lock_conn:
        got = (
            await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)").bindparams(k=key))
        ).scalar()
        if not got:
            return
        try:
            await coro(session, datetime.now(UTC))
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=key))


async def _head_poll() -> None:
    """One git staleness poll, under its own advisory lock (on `_with_lock`'s dedicated
    connection). Run as a task of its own: its git calls can outlast a tick."""
    try:
        async with SessionLocal() as session:
            await _with_lock(session, _LOCK_HEAD_POLL, poll_branch_heads)
    except Exception as exc:
        print(f"[scheduler] head poll error: {exc}", flush=True)


async def scheduler_loop() -> None:
    """Single background loop; each task is advisory-locked + idempotent (§7.5). A 10s tick keeps
    outbound notifications responsive; worker_lost detection is cheap/idempotent at that rate. The
    git staleness poll runs once per `stackd_head_poll_interval` only (§9.6), in a task of its own
    so its git calls never delay the other tasks' ticks; a poll still running when the next one is
    due skips that one."""
    next_head_poll: datetime | None = None
    next_retention: datetime | None = None
    head_poll: asyncio.Task[None] | None = None
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    await _with_lock(session, _LOCK_WORKER_LOST, detect_worker_lost)
                async with SessionLocal() as session:
                    await _with_lock(session, _LOCK_NOTIFY, dispatch_pending)
                async with SessionLocal() as session:
                    await _with_lock(session, _LOCK_VCS, dispatch_vcs)
                async with SessionLocal() as session:
                    await _with_lock(session, _LOCK_DRIFT, detect_drift)
                async with SessionLocal() as session:
                    await _with_lock(session, _LOCK_OIDC_KEYS, retire_keys)
                async with SessionLocal() as session:
                    await _with_lock(session, _LOCK_ROLLOUTS, expire_approvals)
                async with SessionLocal() as session:
                    await _with_lock(session, _LOCK_LOG_ARCHIVE, archive_run_logs)
                now = datetime.now(UTC)
                if next_retention is None or now >= next_retention:
                    next_retention = now + _RETENTION_PERIOD
                    async with SessionLocal() as session:
                        await _with_lock(session, _LOCK_RETENTION, purge_retention)
                if next_head_poll is None or now >= next_head_poll:
                    # Scheduled before it runs, so a failing poll also waits a full interval.
                    interval = get_settings().stackd_head_poll_interval
                    next_head_poll = now + timedelta(seconds=interval)
                    if head_poll is None or head_poll.done():
                        head_poll = asyncio.create_task(_head_poll())
            except Exception as exc:
                print(f"[scheduler] tick error: {exc}", flush=True)
            await asyncio.sleep(_TICK_SECONDS)
    finally:
        if head_poll is not None:
            head_poll.cancel()  # the loop is cancelled at shutdown: don't leave the poll behind
