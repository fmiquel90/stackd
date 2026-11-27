"""Claim resilience (SPECS §7.2): the long-poll holds no transaction while it waits, a run whose job
cannot be built never blocks the queue behind it, the payload build holds no lock on the worker's
row, and the candidate query has an index."""

from __future__ import annotations

import json
import os
import types
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.models.run import Run
from app.oidc.issuer import WorkloadSigningUnavailable
from app.workers import worker_router
from tests.conftest_phase2 import login, make_env, make_stack, register_worker


async def _trigger(client: httpx.AsyncClient, h: dict, env_id: str) -> str:
    r = await client.post(f"/api/v1/environments/{env_id}/runs", headers=h, json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _run(run_id: str) -> Run:
    async with SessionLocal() as session:
        run = await session.get(Run, uuid.UUID(run_id))
        assert run is not None
        return run


async def _env(client: httpx.AsyncClient, admin: dict, name: str) -> str:
    return await make_env(client, admin, await make_stack(client, admin, name), "dev", "dev")


def _worker_id(wh: dict[str, str]) -> uuid.UUID:
    token = wh["Authorization"].removeprefix("Bearer ")
    return uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sub"])


async def test_long_poll_waits_outside_a_transaction(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a claim waits for work, its connection is not idle in transaction; the wait is capped
    server-side; a run queued during the wait is handed out."""
    import asyncio

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-claim-idle")
    env_id = await _env(client, admin, "claim-idle")
    monkeypatch.setattr(get_settings(), "stackd_claim_max_wait_seconds", 6)

    idle_in_txn: list[int] = []
    queued: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        async with SessionLocal() as probe:
            idle_in_txn.append(
                await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
                    )
                )
            )
        if len(idle_in_txn) == 2 and not queued:
            queued.append(await _trigger(client, admin, env_id))
        await asyncio.sleep(0)

    monkeypatch.setattr(worker_router, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    r = await client.post("/worker/v1/jobs/claim?wait=1000", headers=wh)
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == queued[0]
    assert idle_in_txn == [0, 0]

    # Nothing to do: the poll gives up after the cap (6 s = 3 waits of 2 s), not after `wait`.
    idle_in_txn.clear()
    r = await client.post("/worker/v1/jobs/claim?wait=1000", headers=wh)
    assert r.status_code == 204
    assert len(idle_in_txn) == 3


async def test_payload_build_holds_no_lock_on_the_worker(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's row (its heartbeat, worker-lost detection) and a key-share on the env (a new run
    inserted on it) stay available while the job is built."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-claim-locks")
    env_id = await _env(client, admin, "claim-locks")
    run_id = await _trigger(client, admin, env_id)
    real = worker_router.build_job_payload
    taken: list[str] = []

    async def probing_build(session: object, run: Run) -> dict:
        async with SessionLocal() as probe:
            await probe.execute(
                text("SELECT id FROM workers WHERE id = :id FOR UPDATE NOWAIT").bindparams(
                    id=_worker_id(wh)
                )
            )
            taken.append("worker")
            await probe.execute(
                text("SELECT id FROM environments WHERE id = :id FOR KEY SHARE NOWAIT").bindparams(
                    id=uuid.UUID(env_id)
                )
            )
            taken.append("env")
            await probe.rollback()
        return await real(session, run)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_router, "build_job_payload", probing_build)
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == run_id
    assert taken == ["worker", "env"]


async def test_undecryptable_secret_fails_the_run_and_unblocks_the_queue(
    client: httpx.AsyncClient,
) -> None:
    """An unexpected error building the job (here a sealed override that no longer decrypts) fails
    the run closed with a generic error, audited — and the next poll takes the run behind it."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-claim-poison")
    poison = await _trigger(client, admin, await _env(client, admin, "claim-poison-a"))
    behind = await _trigger(client, admin, await _env(client, admin, "claim-poison-b"))
    async with SessionLocal() as session:
        await session.execute(
            update(Run)
            .where(Run.id == uuid.UUID(poison))
            .values(secret_overrides_encrypted=os.urandom(48))
        )
        await session.commit()

    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 204, r.text
    failed = await _run(poison)
    assert failed.state.value == "failed" and failed.error == "job_build_failed:InvalidTag"
    async with SessionLocal() as session:
        audit = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "run.apply_failed",
                    AuditEvent.target_id == uuid.UUID(poison),
                )
            )
        ).scalar_one()
    assert audit.context["reason"] == "internal"

    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == behind


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (WorkloadSigningUnavailable("kms_throttled"), "kms_throttled"),
        (OperationalError("SELECT 1", {}, Exception("connection reset")), "db_unavailable"),
    ],
)
async def test_transient_failure_backs_off_and_lets_the_queue_move(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
) -> None:
    """A transient failure undoes the claim (503, the run keeps its state) and keeps the run out of
    the claim for a growing backoff: the run behind it is claimed meanwhile."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-claim-backoff-{code}")
    stuck = await _trigger(client, admin, await _env(client, admin, f"claim-backoff-a-{code}"))
    behind = await _trigger(client, admin, await _env(client, admin, f"claim-backoff-b-{code}"))
    real = worker_router.build_job_payload

    async def failing_build(session: object, run: Run) -> dict:
        if str(run.id) == stuck:
            raise error
        return await real(session, run)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_router, "build_job_payload", failing_build)
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 503, r.text
    assert r.json()["code"] == code and r.json()["retryable"] is True
    first = await _run(stuck)
    assert first.state.value == "queued" and first.worker_id is None
    assert first.claim_attempts == 1 and first.claimable_after is not None
    delay = (first.claimable_after - datetime.now(UTC)).total_seconds()
    assert 5 < delay <= 10

    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == behind

    async def _backoff_over() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(Run)
                .where(Run.id == uuid.UUID(stuck))
                .values(claimable_after=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    await _backoff_over()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 503
    second = await _run(stuck)
    assert second.claim_attempts == 2
    assert second.claimable_after is not None
    assert 15 < (second.claimable_after - datetime.now(UTC)).total_seconds() <= 20

    monkeypatch.setattr(worker_router, "build_job_payload", real)
    await _backoff_over()
    other = await register_worker(client, admin, f"pool-claim-backoff-2-{code}")
    r = await client.post("/worker/v1/jobs/claim", headers=other)
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == stuck
    claimed = await _run(stuck)
    assert claimed.claim_attempts == 0 and claimed.claimable_after is None


async def test_claim_candidates_use_the_partial_index(client: httpx.AsyncClient) -> None:
    """With a history of terminal runs, the claim reads the queued/confirmed runs through
    `ix_runs_claimable` instead of scanning `runs`."""
    from app.workers.claim import _SELECT_CANDIDATE

    admin = await login(client, "admin")
    await register_worker(client, admin, "pool-claim-index")
    env_id = await _env(client, admin, "claim-index")
    await _trigger(client, admin, env_id)
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO runs (environment_id, type, state, triggered_by, created_at,"
                " finished_at) SELECT :env, 'tracked', 'finished', 'manual',"
                " now() - make_interval(secs => g), now() - make_interval(secs => g)"
                " FROM generate_series(1, 20000) g"
            ).bindparams(env=uuid.UUID(env_id))
        )
        await session.commit()
        await session.execute(text("ANALYZE runs"))
        plan = (
            await session.execute(
                text("EXPLAIN " + _SELECT_CANDIDATE.text).bindparams(
                    wid=uuid.uuid4(),
                    labels=json.dumps({}),
                    tools=None,
                    affinity=60,
                )
            )
        ).scalars()
        assert "ix_runs_claimable" in "\n".join(plan)


async def test_api_connections_carry_the_server_side_timeouts(client: httpx.AsyncClient) -> None:
    async with SessionLocal() as session:
        idle = await session.scalar(text("SHOW idle_in_transaction_session_timeout"))
        lock = await session.scalar(text("SHOW lock_timeout"))
        statement = await session.scalar(text("SHOW statement_timeout"))
    assert (idle, lock, statement) == ("5min", "30s", "0")
