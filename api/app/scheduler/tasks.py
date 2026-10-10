from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crypto import decrypt
from app.db import SessionLocal, get_engine
from app.drift.service import detect_drift
from app.enums import ACTIVE_STATES, RepoAuthKind, RunEventActor, RunState, WorkerStatus
from app.logging import get_logger
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.worker import Worker
from app.notifications.dispatcher import dispatch_pending
from app.runs.transition import transition
from app.stacks.git import ls_remote_sha
from app.vcs.dispatcher import dispatch_vcs
from app.workers.claim import holds_apply_credentials

# Distinct advisory-lock keys so each periodic task runs once across replicas (§7.5).
_LOCK_WORKER_LOST = 74001
_LOCK_NOTIFY = 74002
_LOCK_VCS = 74003
_LOCK_DRIFT = 74004
_LOCK_HEAD_POLL = 74005
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
                now = datetime.now(UTC)
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
