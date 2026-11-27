from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest_phase2 import event, login, make_env, register_worker

SECRET = "whsecret-vcs"
PAT = "ghp_testtoken"


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _stack(
    client: httpx.AsyncClient, admin: dict[str, str], name: str
) -> tuple[str, str, str]:
    # Unique repo per stack: the webhook matches by repo_url, and stacks persist across tests.
    repo = f"https://github.com/acme/{name}"
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"name": name, "repo_url": repo, "tool_version": "1.12.0"},
    )
    sid = r.json()["id"]
    await client.patch(
        f"/api/v1/stacks/{sid}", headers=admin, json={"webhook_secret": SECRET, "repo_secret": PAT}
    )
    env_id = await make_env(client, admin, sid, "main", "dev")
    return sid, env_id, repo


async def _pr(
    client: httpx.AsyncClient, repo: str, *, sha: str, number: int, action: str = "opened"
) -> httpx.Response:
    payload = {
        "action": action,
        "number": number,
        "repository": {"clone_url": repo},
        "pull_request": {"number": number, "base": {"ref": "main"}, "head": {"sha": sha}},
    }
    body = json.dumps(payload).encode()
    return await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sig(body),
            "X-GitHub-Delivery": str(uuid.uuid4()),
        },
    )


def test_parse_owner_repo() -> None:
    from app.vcs.github import parse_owner_repo

    assert parse_owner_repo("https://github.com/acme/infra") == ("acme", "infra")
    assert parse_owner_repo("https://github.com/acme/infra.git") == ("acme", "infra")
    assert parse_owner_repo("git@github.com:acme/infra.git") == ("acme", "infra")
    assert parse_owner_repo("https://github.com/acme/infra/") == ("acme", "infra")
    assert parse_owner_repo("") is None


async def test_pull_request_persists_vcs_metadata(client: httpx.AsyncClient) -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.run import Run

    admin = await login(client, "admin")
    _sid, env_id, repo = await _stack(client, admin, "vcs-meta")
    resp = await _pr(client, repo, sha="prsha-meta", number=42)
    assert resp.status_code == 200

    async with SessionLocal() as s:
        run = (
            await s.execute(select(Run).where(Run.environment_id == uuid.UUID(env_id)))
        ).scalar_one()
    assert run.pr_number == 42
    assert run.vcs_provider == "github"
    assert run.vcs_head_sha == "prsha-meta"


async def test_pr_closed_cancels_proposed_run(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    _sid, env_id, repo = await _stack(client, admin, "vcs-close")
    assert (await _pr(client, repo, sha="prsha-close", number=8)).status_code == 200
    assert (
        await _pr(client, repo, sha="prsha-close", number=8, action="closed")
    ).status_code == 200

    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    assert runs[0]["state"] == "canceled"


async def test_dispatch_posts_status_and_comment(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import SessionLocal
    from app.models.run import Run
    from app.vcs import dispatcher, github

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "vcs-pool")
    _sid, env_id, repo = await _stack(client, admin, "vcs-dispatch")
    assert (await _pr(client, repo, sha="prsha-disp", number=9)).status_code == 200

    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    run_id = runs[0]["id"]
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    await event(client, wh, run_id, "phase_started", phase="planning")
    await event(
        client,
        wh,
        run_id,
        "phase_finished",
        result={"has_changes": True, "summary": {"add": 2, "change": 1, "destroy": 0}},
    )

    statuses: list[tuple] = []
    comments: list[tuple] = []
    txn_open: list[bool] = []  # session.in_transaction() seen during each GitHub call
    new_ids = itertools.count(555)  # each creation gets a fresh id: a duplicate would show as 556

    async def fake_set_status(
        token, owner, repo, sha, *, state, target_url, context, description, http
    ):  # type: ignore[no-untyped-def]
        txn_open.append(s.in_transaction())
        statuses.append((owner, repo, sha, state, context))

    async def fake_upsert(token, owner, repo, pr_number, body, comment_id, *, http):  # type: ignore[no-untyped-def]
        txn_open.append(s.in_transaction())
        comments.append((pr_number, body, comment_id))
        return comment_id or next(new_ids)

    monkeypatch.setattr(github, "set_status", fake_set_status)
    monkeypatch.setattr(github, "upsert_comment", fake_upsert)

    async with SessionLocal() as s:
        processed = await dispatcher.dispatch_vcs(s, datetime.now(UTC))
    assert processed == 2  # planning (pending) + finished (success)
    assert ("acme", "vcs-dispatch", "prsha-disp", "success", "stackd/plan") in statuses
    assert any(state == "pending" for *_, state, _ in statuses)
    assert comments[0][0] == 9
    assert "+2 ~1 -0" in comments[-1][1]
    # Both rows were in one batch: the first creates the comment, the second edits that one.
    assert [c[2] for c in comments] == [None, 555]
    # No DB transaction held across the HTTP calls.
    assert txn_open and not any(txn_open)

    async with SessionLocal() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run.vcs_comment_id == 555
        assert await dispatcher.dispatch_vcs(s, datetime.now(UTC)) == 0


async def test_dispatch_bad_secret_fails_only_its_row(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from prometheus_client import REGISTRY
    from sqlalchemy import select, update

    from app.config import get_settings
    from app.db import SessionLocal
    from app.models.run import Run
    from app.models.stack import Stack
    from app.models.vcs import VcsOutbox
    from app.vcs import dispatcher, github

    admin = await login(client, "admin")
    bad_sid, bad_env, bad_repo = await _stack(client, admin, "vcs-badsecret")
    _sid, good_env, good_repo = await _stack(client, admin, "vcs-goodsecret")
    assert (await _pr(client, bad_repo, sha="prsha-bad", number=11)).status_code == 200
    assert (await _pr(client, good_repo, sha="prsha-good", number=12)).status_code == 200
    bad_run = (await client.get(f"/api/v1/environments/{bad_env}/runs", headers=admin)).json()[0]
    good_run = (await client.get(f"/api/v1/environments/{good_env}/runs", headers=admin)).json()[0]

    # Batch order: good, bad, good — rows on both sides of the bad one must go through.
    for rid in (good_run["id"], bad_run["id"], good_run["id"]):
        assert (
            await client.post(f"/api/v1/runs/{rid}/vcs/resync", headers=admin)
        ).status_code == 202
    async with SessionLocal() as s:  # e.g. key rotated / corrupt blob → AES-GCM InvalidTag
        await s.execute(
            update(Stack)
            .where(Stack.id == uuid.UUID(bad_sid))
            .values(repo_secret_encrypted=b"\x00" * 40)
        )
        await s.commit()

    statuses: list[str] = []
    comments: list[int | None] = []

    async def fake_set_status(
        token, owner, repo, sha, *, state, target_url, context, description, http
    ):  # type: ignore[no-untyped-def]
        statuses.append(sha)

    async def fake_upsert(token, owner, repo, pr_number, body, comment_id, *, http):  # type: ignore[no-untyped-def]
        comments.append(comment_id)
        return comment_id or 777

    monkeypatch.setattr(github, "set_status", fake_set_status)
    monkeypatch.setattr(github, "upsert_comment", fake_upsert)

    async with SessionLocal() as s:
        assert await dispatcher.dispatch_vcs(s, datetime.now(UTC)) == 3
    assert statuses == ["prsha-good", "prsha-good"]
    assert comments == [None, 777]

    async def outbox(run_id: str) -> list[VcsOutbox]:
        async with SessionLocal() as s:
            q = select(VcsOutbox).where(VcsOutbox.run_id == uuid.UUID(run_id))
            return list((await s.execute(q)).scalars().all())

    assert all(r.sent_at is not None for r in await outbox(good_run["id"]))
    [bad_row] = await outbox(bad_run["id"])
    assert bad_row.sent_at is None and bad_row.attempts == 1
    async with SessionLocal() as s:
        assert (await s.get(Run, uuid.UUID(good_run["id"]))).vcs_comment_id == 777

    # Backing off: the failed row is not due again right away.
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_vcs(s, datetime.now(UTC)) == 0

    # The bad row alone is retried, each time it is due, until its attempts are spent; the good
    # rows are never re-posted. The last failure is reported at error level and counted.
    max_attempts = get_settings().stackd_outbox_max_attempts
    dead_before = (
        REGISTRY.get_sample_value("stackd_outbox_dead_letters_total", {"outbox": "vcs"}) or 0.0
    )
    later = datetime.now(UTC)
    caplog.clear()
    for _ in range(max_attempts - 1):
        later += timedelta(hours=2)
        async with SessionLocal() as s:
            assert await dispatcher.dispatch_vcs(s, later) == 1
    [bad_row] = await outbox(bad_run["id"])
    assert bad_row.sent_at is None and bad_row.attempts == max_attempts
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_vcs(s, later + timedelta(days=1)) == 0
    assert statuses == ["prsha-good", "prsha-good"]
    dead = [r for r in caplog.records if getattr(r, "event", None) == "vcs.dead_lettered"]
    assert [(r.levelname, r.run_id, r.attempts) for r in dead] == [
        ("ERROR", bad_run["id"], max_attempts)
    ]
    assert (
        REGISTRY.get_sample_value("stackd_outbox_dead_letters_total", {"outbox": "vcs"})
        == dead_before + 1
    )


async def test_dispatch_honours_rate_limit_and_drops_superseded_retry(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GitHub rate limit defers the row until the limit resets; by then a newer row of the same
    run has posted the later status, so the stale retry is dropped instead of rolling it back."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.vcs import VcsOutbox
    from app.vcs import dispatcher, github

    admin = await login(client, "admin")
    _sid, env_id, repo = await _stack(client, admin, "vcs-ratelimit")
    assert (await _pr(client, repo, sha="prsha-rl", number=21)).status_code == 200
    run_id = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()[0][
        "id"
    ]
    assert (
        await client.post(f"/api/v1/runs/{run_id}/vcs/resync", headers=admin)
    ).status_code == 202

    now = datetime.now(UTC)
    reset = int(now.timestamp()) + 600
    statuses: list[str] = []

    async def limited(token, owner, repo, sha, *, state, target_url, context, description, http):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", "https://api.github.com/x")
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
        response = httpx.Response(403, headers=headers, request=request)
        raise httpx.HTTPStatusError("403 rate limited", request=request, response=response)

    async def fake_upsert(token, owner, repo, pr_number, body, comment_id, *, http):  # type: ignore[no-untyped-def]
        return comment_id or 901

    monkeypatch.setattr(github, "set_status", limited)
    monkeypatch.setattr(github, "upsert_comment", fake_upsert)
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_vcs(s, now) == 1

    async def rows() -> list[VcsOutbox]:
        async with SessionLocal() as s:
            q = (
                select(VcsOutbox)
                .where(VcsOutbox.run_id == uuid.UUID(run_id))
                .order_by(VcsOutbox.created_at)
            )
            return list((await s.execute(q)).scalars().all())

    [stale] = await rows()
    assert stale.sent_at is None
    assert stale.next_attempt_at >= datetime.fromtimestamp(reset, UTC) - timedelta(seconds=1)

    async def fake_set_status(
        token, owner, repo, sha, *, state, target_url, context, description, http
    ):  # type: ignore[no-untyped-def]
        statuses.append(description)

    monkeypatch.setattr(github, "set_status", fake_set_status)
    assert (
        await client.post(f"/api/v1/runs/{run_id}/vcs/resync", headers=admin)
    ).status_code == 202
    async with SessionLocal() as s:  # only the new row is due
        assert await dispatcher.dispatch_vcs(s, now + timedelta(seconds=60)) == 1
    assert len(statuses) == 1
    async with SessionLocal() as s:  # the limit reset: the stale row is due, and superseded
        assert await dispatcher.dispatch_vcs(s, now + timedelta(seconds=700)) == 1
    assert len(statuses) == 1
    assert all(r.sent_at is not None for r in await rows())


async def test_resync_requeues(client: httpx.AsyncClient) -> None:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models.vcs import VcsOutbox

    admin = await login(client, "admin")
    _sid, env_id, repo = await _stack(client, admin, "vcs-resync")
    assert (await _pr(client, repo, sha="prsha-rs", number=3)).status_code == 200
    run_id = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()[0][
        "id"
    ]

    r = await client.post(f"/api/v1/runs/{run_id}/vcs/resync", headers=admin)
    assert r.status_code == 202

    async with SessionLocal() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(VcsOutbox)
                .where(VcsOutbox.run_id == uuid.UUID(run_id))
            )
        ).scalar_one()
    assert n == 1
