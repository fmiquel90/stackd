"""Bounded read paths: run list pages, audit export stream, staleness probe, /metrics, webhook
matching, and the indexes behind them (migration 0046)."""

from __future__ import annotations

import csv
import io
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text

from tests.conftest_phase2 import login, make_env, make_stack


async def _seed_runs(env_id: str, rows: list[dict]) -> list[str]:
    """Fixture rows straight into `runs` (no state machine: these are history, not live runs)."""
    from app.db import SessionLocal
    from app.enums import RunState, RunType, TriggeredBy
    from app.models.run import Run

    async with SessionLocal() as session:
        runs = [
            Run(
                environment_id=uuid.UUID(env_id),
                type=r.get("type", RunType.tracked),
                state=r.get("state", RunState.finished),
                triggered_by=TriggeredBy.manual,
                commit_sha=r.get("sha"),
                plan_options=r.get("plan_options"),
                plan_summary={"add": 1, "change": 0, "destroy": 0},
                check_results={"results": [{"name": "c", "status": "ok"}]},
                variable_provenance={"x": {"source": "env"}},
                created_at=r.get("created_at", datetime.now(UTC)),
                finished_at=r.get("finished_at"),
            )
            for r in rows
        ]
        session.add_all(runs)
        await session.commit()
        return [str(r.id) for r in runs]


async def _env(client: httpx.AsyncClient, admin: dict[str, str], tag: str) -> str:
    return await make_env(
        client, admin, await make_stack(client, admin, f"{tag}-stack"), "dev", "dev"
    )


async def test_run_list_pages_newest_first_with_a_cursor(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    env_id = await _env(client, admin, "page")
    base = datetime.now(UTC) - timedelta(hours=1)
    # Two runs share a created_at: the id breaks the tie, so a page boundary between them loses
    # neither.
    stamps = [
        base,
        base + timedelta(seconds=1),
        base + timedelta(seconds=1),
        base + timedelta(seconds=2),
    ]
    await _seed_runs(env_id, [{"created_at": t} for t in stamps])
    url = f"/api/v1/environments/{env_id}/runs"

    everything = await client.get(url, headers=admin)
    assert everything.status_code == 200
    assert "x-next-cursor" not in everything.headers
    expected = [r["id"] for r in everything.json()]
    assert len(expected) == 4

    seen: list[str] = []
    params: dict[str, str | int] = {"limit": 1}
    while True:
        page = await client.get(url, headers=admin, params=params)
        assert page.status_code == 200
        assert len(page.json()) == 1
        seen += [r["id"] for r in page.json()]
        cursor = page.headers.get("x-next-cursor")
        if cursor is None:
            break
        params["before"] = cursor
    assert seen == expected


async def test_run_list_default_cap_and_limit_bounds(client: httpx.AsyncClient) -> None:
    from app.runs.router import RUN_LIST_DEFAULT_LIMIT

    admin = await login(client, "admin")
    env_id = await _env(client, admin, "cap")
    base = datetime.now(UTC) - timedelta(days=1)
    await _seed_runs(
        env_id,
        [{"created_at": base + timedelta(seconds=i)} for i in range(RUN_LIST_DEFAULT_LIMIT + 1)],
    )
    url = f"/api/v1/environments/{env_id}/runs"
    first = await client.get(url, headers=admin)
    assert len(first.json()) == RUN_LIST_DEFAULT_LIMIT
    rest = await client.get(url, headers=admin, params={"before": first.headers["x-next-cursor"]})
    assert len(rest.json()) == 1
    assert "x-next-cursor" not in rest.headers
    assert rest.json()[0]["created_at"] < first.json()[-1]["created_at"]

    for bad in (0, 501):
        assert (await client.get(url, headers=admin, params={"limit": bad})).status_code == 422


async def test_run_list_rejects_a_foreign_cursor(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    env_a = await _env(client, admin, "cur-a")
    env_b = await _env(client, admin, "cur-b")
    [run_b] = await _seed_runs(env_b, [{}])
    url = f"/api/v1/environments/{env_a}/runs"
    for cursor in (run_b, str(uuid.uuid4())):
        resp = await client.get(url, headers=admin, params={"before": cursor})
        assert resp.status_code == 400, resp.text


async def test_run_list_summary_view_drops_the_heavy_json(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    env_id = await _env(client, admin, "slim")
    await _seed_runs(env_id, [{"sha": "abc"}])
    url = f"/api/v1/environments/{env_id}/runs"

    [full] = (await client.get(url, headers=admin)).json()
    assert full["check_results"] == {"results": [{"name": "c", "status": "ok"}]}
    assert full["variable_provenance"] == {"x": {"source": "env"}}
    assert "resolved_inputs" not in full

    [slim] = (await client.get(url, headers=admin, params={"view": "summary"})).json()
    assert "check_results" not in slim and "variable_provenance" not in slim
    assert slim["plan_summary"] == {"add": 1, "change": 0, "destroy": 0}
    assert {k: v for k, v in full.items() if k in slim} == slim

    assert (await client.get(url, headers=admin, params={"view": "nope"})).status_code == 422


async def test_audit_export_streams_every_row_across_batches(client: httpx.AsyncClient) -> None:
    from app.audit.router import _EXPORT_BATCH
    from app.db import SessionLocal

    admin = await login(client, "admin")
    action = f"test.export_{uuid.uuid4().hex[:8]}"
    n = _EXPORT_BATCH * 2 + 7
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO audit_events (actor_kind, actor_email, action, context, created_at) "
                "SELECT 'system', NULL, :action, jsonb_build_object('i', i), "
                "now() - make_interval(secs => i) FROM generate_series(1, :n) AS i"
            ).bindparams(action=action, n=n)
        )
        await session.commit()

    resp = await client.get("/api/v1/audit/export", headers=admin, params={"action": action})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == "attachment; filename=audit.csv"
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["created_at", "actor_email", "action", "target_kind", "target_id", "context"]
    body = rows[1:]
    assert len(body) == n
    assert [r[5] for r in body] == [f'{{"i": {i}}}' for i in range(1, n + 1)]  # newest first

    empty = await client.get("/api/v1/audit/export", headers=admin, params={"action": "none.x"})
    assert list(csv.reader(io.StringIO(empty.text))) == [rows[0]]


async def test_last_applied_commit_skips_targeted_failed_and_proposed_runs(
    client: httpx.AsyncClient,
) -> None:
    from app.db import SessionLocal
    from app.enums import RunState, RunType
    from app.models.environment import Environment

    admin = await login(client, "admin")
    stack_id = await make_stack(client, admin, "applied-stack")
    env_id = await make_env(client, admin, stack_id, "dev", "dev")
    untouched = await make_env(client, admin, stack_id, "staging", "staging")
    t = datetime.now(UTC) - timedelta(hours=1)
    await _seed_runs(
        env_id,
        [
            {"sha": "aaa", "finished_at": t},
            {
                "sha": "bbb",
                "finished_at": t + timedelta(minutes=1),
                "plan_options": {"targets": ["a.b"]},
            },
            {"sha": "ccc", "finished_at": t + timedelta(minutes=2), "state": RunState.failed},
            {"sha": "ddd", "finished_at": t + timedelta(minutes=3), "type": RunType.proposed},
        ],
    )

    async def stale_at(head: str) -> dict[str, bool]:
        async with SessionLocal() as session:
            for e in (env_id, untouched):
                env = await session.get(Environment, uuid.UUID(e))
                assert env is not None
                env.head_sha = head
            await session.commit()
        listed = await client.get(f"/api/v1/stacks/{stack_id}/environments", headers=admin)
        return {e["id"]: e["stale"] for e in listed.json()}

    assert await stale_at("aaa") == {env_id: False, untouched: False}
    assert await stale_at("bbb") == {env_id: True, untouched: False}


def _gauge(body: str, state: str) -> float:
    m = re.search(rf'^stackd_runs_total{{state="{state}"}} (\S+)$', body, re.M)
    assert m, body
    return float(m.group(1))


async def test_metrics_count_live_runs_each_scrape_and_cache_terminal_ones(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.enums import RunState
    from app.observability import router as obs

    monkeypatch.setattr(obs, "_terminal_counts", None)
    admin = await login(client, "admin")
    env_id = await _env(client, admin, "metrics")
    await _seed_runs(env_id, [{"state": RunState.queued}, {"state": RunState.finished}])

    body = (await client.get("/metrics")).text
    assert _gauge(body, "queued") == 1
    assert _gauge(body, "finished") == 1
    assert re.search(r"^stackd_queue_depth 1\.0$", body, re.M)

    await _seed_runs(env_id, [{"state": RunState.queued}, {"state": RunState.finished}])
    body = (await client.get("/metrics")).text
    assert _gauge(body, "queued") == 2  # live: counted at every scrape
    assert _gauge(body, "finished") == 1  # terminal: cached

    monkeypatch.setattr(obs, "_terminal_counts", None)  # the TTL ran out
    assert _gauge((await client.get("/metrics")).text, "finished") == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/demo",
        "https://github.com/acme/demo.git",
        "https://github.com/acme/demo/",
        "https://github.com/acme/demo//",
        "https://github.com/acme/demo.git/",
        "https://github.com/acme/demo/.git",
        "https://github.com/acme/demo.GIT",
        "git@github.com:acme/demo.git",
        ".git",
        "",
    ],
)
async def test_sql_repo_url_normalization_matches_python(url: str, _database: str) -> None:
    from sqlalchemy import literal

    from app.db import SessionLocal
    from app.webhooks.router import _normalize, _normalized_sql

    async with SessionLocal() as session:
        assert (await session.scalar(select(_normalized_sql(literal(url))))) == _normalize(url)


async def test_webhook_matches_a_stack_registered_with_a_trailing_slash(
    client: httpx.AsyncClient,
) -> None:
    import hashlib
    import hmac
    import json

    admin = await login(client, "admin")
    repo = f"https://github.com/acme/slash-{uuid.uuid4().hex[:6]}"
    stack = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"name": "wh-slash", "repo_url": repo + "/", "tool_version": "1.12.0"},
    )
    await client.patch(
        f"/api/v1/stacks/{stack.json()['id']}", headers=admin, json={"webhook_secret": "s3cret"}
    )
    # Same repo, no secret: never a candidate.
    await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"name": "wh-nosecret", "repo_url": repo + ".git", "tool_version": "1.12.0"},
    )
    body = json.dumps({"repository": {"html_url": repo}}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    resp = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["matched"] == 1


def test_read_path_indexes_migrate_down_and_up(_database: str) -> None:
    import asyncio

    from alembic import command
    from alembic.config import Config

    ours = {
        "ix_runs_env_created_id",
        "ix_runs_last_finished_tracked",
        "ix_runs_non_terminal_state",
        "ix_user_notifications_run_id",
        "ix_user_notifications_comment_id",
        "ix_notification_outbox_run_id",
        "ix_vcs_outbox_run_id",
        "ix_env_rollout_members_run_id",
    }

    async def names() -> set[str]:
        # A throwaway engine: the shared one's pool belongs to the session's event loop.
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(os.environ["DATABASE_URL"])
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT indexname FROM pg_indexes"))
            found = set(rows.scalars())
        await engine.dispose()
        return found

    assert ours <= asyncio.run(names())
    assert "ix_runs_environment_id" not in asyncio.run(names())
    # No ini file: its logging section would reconfigure logging and disable the app's loggers for
    # the tests that run after this one.
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    command.downgrade(cfg, "0046-1")
    after_down = asyncio.run(names())
    assert not ours & after_down and "ix_runs_environment_id" in after_down
    command.upgrade(cfg, "head")
    assert ours <= asyncio.run(names())
