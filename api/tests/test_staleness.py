"""Git staleness (§9.6): `stale` on the environment API, and the fallback branch-head polling
task of the scheduler (§7.5)."""

from __future__ import annotations

import contextlib
import json
import logging
import types
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest_phase2 import event, login, make_env, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


def _repo() -> str:
    return f"file:///repos/staleness-{uuid.uuid4().hex[:8]}"


async def _stack(
    client: httpx.AsyncClient, h: dict[str, str], repo_url: str, *, token: str | None = None
) -> str:
    body: dict[str, str] = {
        "name": f"stale-{uuid.uuid4().hex[:8]}",
        "repo_url": repo_url,
        "tool_version": "1.12.0",
    }
    if token:
        body |= {"repo_auth_kind": "token", "repo_secret": token}
    r = await client.post("/api/v1/stacks", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _env(
    client: httpx.AsyncClient, h: dict[str, str], stack: str, name: str, branch: str
) -> str:
    r = await client.post(
        f"/api/v1/stacks/{stack}/environments",
        headers=h,
        json={"name": name, "tier": "dev", "branch": branch},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _run(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    wh: dict[str, str],
    env_id: str,
    sha: str,
    *,
    run_type: str = "tracked",
    changes: bool = True,
) -> str:
    """Drive a run on `sha` to `finished`: through an apply when the plan has changes (tracked),
    plan-only otherwise (empty plan, or a proposed run)."""
    run = (
        await client.post(
            f"/api/v1/environments/{env_id}/runs",
            headers=admin,
            json={"commit_sha": sha, "type": run_type},
        )
    ).json()
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(
        client,
        wh,
        run["id"],
        "phase_finished",
        result=CHANGES if changes else {"has_changes": False},
    )
    if changes and run_type == "tracked":
        assert (await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)).is_success
        assert (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["phase"] == "apply"
        await event(client, wh, run["id"], "phase_finished")
    detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert detail["state"] == "finished"
    return run["id"]


async def _set_head(env_id: str, sha: str, *, at: datetime | None = None) -> None:
    """Record a branch head on the env as a push webhook / manual refresh does."""
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.environment import Environment

    async with SessionLocal() as session:
        await session.execute(
            update(Environment)
            .where(Environment.id == uuid.UUID(env_id))
            .values(head_sha=sha, head_updated_at=at or datetime.now(UTC))
        )
        await session.commit()


async def _get(client: httpx.AsyncClient, h: dict[str, str], env_id: str) -> dict:
    return (await client.get(f"/api/v1/environments/{env_id}", headers=h)).json()


async def test_env_is_stale_when_head_moved_past_last_applied(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-stale-get")
    env_id = await make_env(client, admin, await _stack(client, admin, _repo()), "dev", "dev")

    await _run(client, admin, wh, env_id, "aaa111")  # applied
    await _set_head(env_id, "bbb222")  # the branch moved ahead
    env = await _get(client, admin, env_id)
    assert env["head_sha"] == "bbb222"
    assert env["stale"] is True

    # A plan-only proposed run on the head applies nothing: still stale.
    await _run(client, admin, wh, env_id, "bbb222", run_type="proposed")
    assert (await _get(client, admin, env_id))["stale"] is True

    # A tracked run on the head with an empty plan counts as applied (§9.6): up to date again.
    await _run(client, admin, wh, env_id, "bbb222", changes=False)
    assert (await _get(client, admin, env_id))["stale"] is False


async def test_list_flags_only_envs_behind_head(client: httpx.AsyncClient) -> None:
    """Equal SHAs, or no applied run yet, are not stale; the list computes it in one query."""
    from sqlalchemy import event as sa_event

    from app.db import get_engine

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-stale-list")
    stack = await _stack(client, admin, _repo())
    behind = await make_env(client, admin, stack, "behind", "dev")
    current = await make_env(client, admin, stack, "current", "dev")
    never = await make_env(client, admin, stack, "never-applied", "dev")
    await _run(client, admin, wh, behind, "ccc111")
    await _run(client, admin, wh, current, "ddd222")
    for env_id in (behind, current, never):
        await _set_head(env_id, "ddd222")

    runs_queries: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        if "FROM runs" in statement:
            runs_queries.append(statement)

    engine = get_engine().sync_engine
    sa_event.listen(engine, "before_cursor_execute", _count)
    try:
        listed = (await client.get(f"/api/v1/stacks/{stack}/environments", headers=admin)).json()
    finally:
        sa_event.remove(engine, "before_cursor_execute", _count)

    stale = {e["id"]: e["stale"] for e in listed}
    assert stale == {behind: True, current: False, never: False}
    assert len(runs_queries) == 1  # one query for the whole list, not one per env
    assert (await _get(client, admin, current))["stale"] is False
    assert (await _get(client, admin, never))["stale"] is False


class _FakeLsRemote:
    """Stands in for `ls_remote_sha`: answers only for the repos a test registers (any other repo
    in the shared test DB reads as unreachable, so its envs are left alone), records every call."""

    def __init__(self) -> None:
        self.heads: dict[tuple[str, str], str | Exception] = {}
        self.calls: list[tuple[str, str, str | None, str]] = []
        self.on_call = None  # optional async hook, run before answering

    def mine(self, repo_url: str) -> list[tuple[str, str | None, str]]:
        return [
            (kind, secret, branch) for url, kind, secret, branch in self.calls if url == repo_url
        ]

    async def __call__(self, repo_url, auth_kind, secret, branch):  # type: ignore[no-untyped-def]
        self.calls.append((repo_url, auth_kind.value, secret, branch))
        if self.on_call is not None:
            await self.on_call()
        head = self.heads.get((repo_url, branch))
        if isinstance(head, Exception):
            raise head
        return head


@pytest.fixture
def ls_remote(monkeypatch: pytest.MonkeyPatch) -> _FakeLsRemote:
    from app.scheduler import tasks

    fake = _FakeLsRemote()
    monkeypatch.setattr(tasks, "ls_remote_sha", fake)
    return fake


async def _poll(now: datetime) -> int:
    from app.db import SessionLocal
    from app.scheduler.tasks import poll_branch_heads

    async with SessionLocal() as session:
        return await poll_branch_heads(session, now)


async def test_poll_updates_heads_once_per_repo_branch(
    client: httpx.AsyncClient, ls_remote: _FakeLsRemote
) -> None:
    """One `ls-remote` per (repo, branch) however many envs — and stacks — track it, with the
    stack's decrypted token; every env tracking the branch gets the head, and turns stale."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-stale-poll")
    repo = _repo()
    # Two stacks on the same repo (one repo serving several stacks, §3.1), same token — each
    # encrypted separately, so the dedup compares the decrypted credentials.
    stack_a = await _stack(client, admin, repo, token="tok-shared")
    stack_b = await _stack(client, admin, repo, token="tok-shared")
    a_dev = await _env(client, admin, stack_a, "dev", "main")
    a_staging = await _env(client, admin, stack_a, "staging", "main")
    a_prod = await _env(client, admin, stack_a, "prod", "release")
    b_dev = await _env(client, admin, stack_b, "dev", "main")
    await _run(client, admin, wh, a_dev, "old111")
    ls_remote.heads = {(repo, "main"): "new222", (repo, "release"): "rel333"}

    assert await _poll(datetime.now(UTC)) == 4

    assert sorted(ls_remote.mine(repo)) == [
        ("token", "tok-shared", "main"),
        ("token", "tok-shared", "release"),
    ]
    heads = {e: (await _get(client, admin, e))["head_sha"] for e in (a_dev, a_staging, b_dev)}
    assert heads == {a_dev: "new222", a_staging: "new222", b_dev: "new222"}
    assert (await _get(client, admin, a_prod))["head_sha"] == "rel333"
    assert (await _get(client, admin, a_dev))["stale"] is True


async def test_poll_survives_a_failing_repo(
    client: httpx.AsyncClient, ls_remote: _FakeLsRemote, caplog: pytest.LogCaptureFixture
) -> None:
    """A repo that errors out (or has no readable head) is skipped and logged without its token;
    the other repos are still polled. No DB transaction is held across the git calls."""
    from app.db import SessionLocal
    from app.scheduler.tasks import poll_branch_heads

    admin = await login(client, "admin")
    broken, gone, healthy = _repo(), _repo(), _repo()
    token = "tok-must-not-leak"
    broken_env = await _env(
        client, admin, await _stack(client, admin, broken, token=token), "e", "main"
    )
    gone_env = await _env(client, admin, await _stack(client, admin, gone), "e", "main")
    healthy_env = await _env(client, admin, await _stack(client, admin, healthy), "e", "main")
    ls_remote.heads = {
        (broken, "main"): RuntimeError(f"fatal: https://x-access-token:{token}@host/repo"),
        (healthy, "main"): "fff999",
    }  # `gone` answers None (unreachable, or the branch was deleted)

    async with SessionLocal() as session:

        async def _no_txn() -> None:
            assert not session.in_transaction()

        ls_remote.on_call = _no_txn
        with caplog.at_level(logging.WARNING, logger="stackd.scheduler"):
            assert await poll_branch_heads(session, datetime.now(UTC)) == 1

    assert ls_remote.mine(broken) and ls_remote.mine(gone) and ls_remote.mine(healthy)
    assert (await _get(client, admin, healthy_env))["head_sha"] == "fff999"
    assert (await _get(client, admin, broken_env))["head_sha"] is None
    assert (await _get(client, admin, gone_env))["head_sha"] is None
    failed = [
        r for r in caplog.records if getattr(r, "event", None) == "scheduler.head_poll_failed"
    ]
    assert any(broken_env in r.environment_ids for r in failed)
    assert any(gone_env in r.environment_ids for r in failed)
    for record in caplog.records:
        assert token not in json.dumps(record.__dict__, default=str)


async def test_poll_respects_the_interval(
    client: httpx.AsyncClient, ls_remote: _FakeLsRemote
) -> None:
    """An env whose head was observed within the interval — by a previous poll on any replica, a
    push webhook or a manual refresh — is not polled again; past the interval it is. A head that a
    webhook records while the poll is calling git is not overwritten by the poll."""
    from app.config import get_settings

    admin = await login(client, "admin")
    repo = _repo()
    env_id = await _env(client, admin, await _stack(client, admin, repo), "e", "main")
    ls_remote.heads = {(repo, "main"): "aaa000"}
    interval = timedelta(seconds=get_settings().stackd_head_poll_interval)

    t0 = datetime.now(UTC)
    await _poll(t0)
    assert len(ls_remote.mine(repo)) == 1
    await _poll(t0 + timedelta(seconds=60))  # e.g. another replica's tick shortly after
    assert len(ls_remote.mine(repo)) == 1
    ls_remote.heads = {(repo, "main"): "bbb111"}
    await _poll(t0 + interval)
    assert len(ls_remote.mine(repo)) == 2
    assert (await _get(client, admin, env_id))["head_sha"] == "bbb111"

    # A push webhook just recorded the head: the env is fresh, the poll leaves it alone.
    await _set_head(env_id, "ccc222")
    await _poll(datetime.now(UTC))
    assert len(ls_remote.mine(repo)) == 2

    # A webhook landing while the poll is calling git wins over the (older) polled head.
    async def _webhook() -> None:
        await _set_head(env_id, "ddd333")

    await _set_head(env_id, "ccc222", at=datetime.now(UTC) - 2 * interval)  # due again
    ls_remote.on_call = _webhook
    await _poll(datetime.now(UTC))
    assert len(ls_remote.mine(repo)) == 3
    assert (await _get(client, admin, env_id))["head_sha"] == "ddd333"


async def test_scheduler_loop_polls_heads_at_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop ticks every 10 s, but the head poll runs once per `stackd_head_poll_interval`,
    under its own advisory-lock key."""
    from app.config import get_settings
    from app.scheduler import tasks

    class _Clock:
        t = datetime(2026, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return cls.t

    class _Stop(Exception):
        pass

    ticks: list[datetime] = []

    async def _sleep(seconds: float) -> None:
        ticks.append(_Clock.t)
        if len(ticks) == 13:
            raise _Stop
        _Clock.t += timedelta(seconds=seconds)

    calls: list[tuple[int, datetime]] = []

    async def _with_lock(session, key, coro) -> None:  # type: ignore[no-untyped-def]
        calls.append((key, _Clock.t))

    monkeypatch.setattr(get_settings(), "stackd_head_poll_interval", 60)
    monkeypatch.setattr(tasks, "datetime", _Clock)
    monkeypatch.setattr(tasks, "asyncio", types.SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(tasks, "SessionLocal", contextlib.nullcontext)
    monkeypatch.setattr(tasks, "_with_lock", _with_lock)

    with pytest.raises(_Stop):
        await tasks.scheduler_loop()

    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert ticks[-1] == start + timedelta(seconds=120)  # 13 ticks, 10 s apart
    polls = [t for key, t in calls if key == tasks._LOCK_HEAD_POLL]
    assert polls == [start, start + timedelta(seconds=60), start + timedelta(seconds=120)]
    worker_lost = [t for key, t in calls if key == tasks._LOCK_WORKER_LOST]
    assert len(worker_lost) == 13  # the other tasks still run every tick


async def _idle_in_transaction() -> int:
    """Connections of this database sitting idle inside an open transaction."""
    from sqlalchemy import text

    from app.db import SessionLocal

    async with SessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND state LIKE 'idle in transaction%'"
                )
            )
        ).scalar_one()


async def test_refresh_head_holds_no_transaction_across_git(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manual refresh reads the branch head with no DB transaction open (§9.6), then records
    it — unless the env was re-pointed to another branch meanwhile."""
    from app.environments import router

    fake = _FakeLsRemote()
    monkeypatch.setattr(router, "ls_remote_sha", fake)
    admin = await login(client, "admin")
    repo = _repo()
    env_id = await _env(client, admin, await _stack(client, admin, repo), "e", "main")
    fake.heads = {(repo, "main"): "aaa111", (repo, "other"): "bbb222"}

    idle: list[int] = []

    async def _no_txn() -> None:
        idle.append(await _idle_in_transaction())

    fake.on_call = _no_txn
    r = await client.post(f"/api/v1/environments/{env_id}/refresh-head", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["head_sha"] == "aaa111"
    assert idle == [0]

    # The env is re-pointed to another branch while git is being read: the old branch's new head
    # isn't recorded on it.
    async def _repoint() -> None:
        from sqlalchemy import update

        from app.db import SessionLocal
        from app.models.environment import Environment

        async with SessionLocal() as session:
            await session.execute(
                update(Environment)
                .where(Environment.id == uuid.UUID(env_id))
                .values(branch="other")
            )
            await session.commit()

    fake.heads[(repo, "main")] = "ccc333"
    fake.on_call = _repoint
    r = await client.post(f"/api/v1/environments/{env_id}/refresh-head", headers=admin)
    assert r.status_code == 200, r.text
    assert (r.json()["branch"], r.json()["head_sha"]) == ("other", "aaa111")


async def test_changing_branch_resets_the_recorded_head(client: httpx.AsyncClient) -> None:
    """The recorded head belongs to the tracked branch: re-pointing the env to another branch
    clears it (and the staleness fields), so `stale` isn't computed against the old branch. A PATCH
    that keeps the branch leaves it alone."""
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.environment import Environment

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-stale-branch")
    env_id = await _env(client, admin, await _stack(client, admin, _repo()), "e", "main")
    await _run(client, admin, wh, env_id, "aaa111")
    await _set_head(env_id, "bbb222")
    async with SessionLocal() as session:
        await session.execute(
            update(Environment)
            .where(Environment.id == uuid.UUID(env_id))
            .values(commits_ahead=3, affects_project_root=True)
        )
        await session.commit()

    async def _patch(body: dict) -> dict:
        r = await client.patch(f"/api/v1/environments/{env_id}", headers=admin, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    kept = await _patch({"branch": "main", "position": 1})
    assert (kept["head_sha"], kept["commits_ahead"], kept["stale"]) == ("bbb222", 3, True)

    moved = await _patch({"branch": "release"})
    assert moved["branch"] == "release"
    assert moved["head_sha"] is None
    assert moved["commits_ahead"] is None
    assert moved["affects_project_root"] is None
    assert moved["stale"] is False
    async with SessionLocal() as session:
        env = await session.get(Environment, uuid.UUID(env_id))
        assert env is not None and env.head_updated_at is None  # due at the next head poll
