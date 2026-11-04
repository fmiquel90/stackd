"""Cold log archiving + retention purges (SPECS §5.2, §6.1, §7.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from moto import mock_aws
from sqlalchemy import text

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _finished_run_with_logs(client: httpx.AsyncClient, tag: str) -> tuple[str, dict]:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-{tag}")
    stack = await make_stack(client, admin, f"{tag}-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    for seq, (section, msg) in enumerate(
        [(None, "\x1b[32mPlan: 1 to add\x1b[0m"), ("hook:x", "ok")]
    ):
        resp = await client.post(
            f"/worker/v1/jobs/{run['id']}/logs",
            headers=wh,
            json={
                "phase": "planning",
                "section": section,
                "seq": seq,
                "lines": [{"t": f"2026-01-01T00:00:0{seq}Z", "msg": msg}],
            },
        )
        assert resp.status_code == 202, resp.text
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_finished")
    return run["id"], admin


async def _count_hot(run_id: str) -> int:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM run_logs WHERE run_id = :r").bindparams(
                    r=uuid.UUID(run_id)
                )
            )
        ).scalar_one()


async def test_logs_archived_then_hot_purged_and_served_from_s3(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.runs.logs_archive import archive_run_logs, purge_hot_logs

    with mock_aws():
        run_id, admin = await _finished_run_with_logs(client, "arch")
        hot = (await client.get(f"/api/v1/runs/{run_id}/logs", headers=admin)).json()
        assert len(hot) == 2

        now = datetime.now(UTC)
        async with SessionLocal() as session:
            assert await archive_run_logs(session, now) >= 1
        # Idempotent: nothing left to archive for this run.
        async with SessionLocal() as session:
            assert await archive_run_logs(session, now) == 0

        # Still within the hot window: rows stay.
        async with SessionLocal() as session:
            await purge_hot_logs(session, now)
        assert await _count_hot(run_id) == 2

        async with SessionLocal() as session:
            assert await purge_hot_logs(session, now + timedelta(days=8)) >= 2
        assert await _count_hot(run_id) == 0

        cold = (await client.get(f"/api/v1/runs/{run_id}/logs", headers=admin)).json()
        assert cold == hot  # transparent: same chunks, same order
        after = (
            await client.get(f"/api/v1/runs/{run_id}/logs", headers=admin, params={"after_seq": 0})
        ).json()
        assert [c["seq"] for c in after] == [1]
        one = (
            await client.get(
                f"/api/v1/runs/{run_id}/logs", headers=admin, params={"phase": "planning"}
            )
        ).json()
        assert len(one) == 2


async def test_download_hot_and_cold(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.runs.logs_archive import archive_run_logs, purge_hot_logs

    with mock_aws():
        run_id, admin = await _finished_run_with_logs(client, "dl")
        url = f"/api/v1/runs/{run_id}/logs/download"
        hot = await client.get(url, headers=admin)
        assert hot.status_code == 200
        assert hot.headers["content-type"].startswith("text/plain")
        assert "attachment" in hot.headers["content-disposition"]
        assert "\x1b[32mPlan: 1 to add" in hot.text  # ANSI preserved
        assert "[hook:x] ok" in hot.text

        now = datetime.now(UTC)
        async with SessionLocal() as session:
            await archive_run_logs(session, now)
            await purge_hot_logs(session, now + timedelta(days=8))
        cold = await client.get(url, headers=admin)
        assert cold.text == hot.text
        assert (await client.get(url, headers=admin, params={"phase": "nope"})).status_code == 404


async def test_archive_failure_leaves_run_unarchived(client: httpx.AsyncClient) -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.run import Run
    from app.runs import logs_archive

    with mock_aws():
        run_id, _ = await _finished_run_with_logs(client, "fail")

        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("s3 down")

        original = logs_archive.put_object
        logs_archive.put_object = boom  # type: ignore[assignment]
        try:
            async with SessionLocal() as session:
                await logs_archive.archive_run_logs(session, datetime.now(UTC))
        finally:
            logs_archive.put_object = original  # type: ignore[assignment]
        async with SessionLocal() as session:
            run = (
                await session.execute(select(Run).where(Run.id == uuid.UUID(run_id)))
            ).scalar_one()
            assert run.logs_archived_at is None  # retried next tick, hot rows untouched
        assert await _count_hot(run_id) == 2


async def test_refresh_token_families_purged_when_fully_expired(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.scheduler.tasks import purge_refresh_tokens

    now = datetime.now(UTC)
    async with SessionLocal() as session:
        user_id = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
        if user_id is None:
            await login(client, "admin")
            user_id = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        dead, live = uuid.uuid4(), uuid.uuid4()
        rows = [
            (dead, now - timedelta(days=3)),
            (dead, now - timedelta(days=1)),  # whole family expired
            (live, now - timedelta(days=1)),
            (live, now + timedelta(days=1)),  # one live token keeps the whole family
        ]
        for fam, exp in rows:
            await session.execute(
                text(
                    "INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at)"
                    " VALUES (:u, :f, :h, :e)"
                ).bindparams(u=user_id, f=fam, h=uuid.uuid4().hex, e=exp)
            )
        await session.commit()
        await purge_refresh_tokens(session, now)
        left = (
            await session.execute(
                text(
                    "SELECT family_id, count(*) FROM refresh_tokens WHERE family_id IN (:a, :b)"
                    " GROUP BY family_id"
                ).bindparams(a=dead, b=live)
            )
        ).all()
    assert {(r[0], r[1]) for r in left} == {(live, 2)}


async def test_audit_purge_needs_the_distinct_role(client: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError

    from app.config import get_settings
    from app.db import SessionLocal
    from app.scheduler.tasks import purge_audit_events

    now = datetime.now(UTC)
    async with SessionLocal() as session:
        has_role = (
            await session.execute(text("SELECT 1 FROM pg_roles WHERE rolname='stackd_audit_purge'"))
        ).scalar()
        assert has_role, "migration 0031 should provision the role"
        await session.execute(
            text(
                "INSERT INTO audit_events (actor_kind, action, created_at)"
                " VALUES ('system','test.old', :t), ('system','test.recent', :n)"
            ).bindparams(t=now - timedelta(days=800), n=now)
        )
        await session.commit()
        # The application role (and anyone else) still cannot delete: the trigger holds.
        try:
            await session.execute(text("DELETE FROM audit_events WHERE action='test.old'"))
            raise AssertionError("delete should have been refused")
        except DBAPIError:
            await session.rollback()
        await session.execute(text("ALTER ROLE stackd_audit_purge LOGIN PASSWORD 'purge'"))
        await session.commit()

    # No URL configured: nothing happens.
    assert await purge_audit_events(now) == 0

    purge_url = make_url(get_settings().database_url).set(
        username="stackd_audit_purge", password="purge"
    )
    monkeypatch.setenv(
        "STACKD_AUDIT_PURGE_DATABASE_URL", purge_url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    try:
        assert await purge_audit_events(now) >= 1
    finally:
        monkeypatch.delenv("STACKD_AUDIT_PURGE_DATABASE_URL")
        get_settings.cache_clear()

    async with SessionLocal() as session:
        actions = {
            r[0] for r in (await session.execute(text("SELECT action FROM audit_events"))).all()
        }
    assert "test.old" not in actions
    assert "test.recent" in actions
    assert "audit.purged" in actions  # the purge is itself audited, in the same transaction
