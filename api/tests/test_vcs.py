from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

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
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)},
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

    async def fake_set_status(token, owner, repo, sha, *, state, target_url, context, description):  # type: ignore[no-untyped-def]
        statuses.append((owner, repo, sha, state, context))

    async def fake_upsert(token, owner, repo, pr_number, body, comment_id):  # type: ignore[no-untyped-def]
        comments.append((pr_number, body, comment_id))
        return 555

    monkeypatch.setattr(github, "set_status", fake_set_status)
    monkeypatch.setattr(github, "upsert_comment", fake_upsert)

    async with SessionLocal() as s:
        processed = await dispatcher.dispatch_vcs(s, datetime.now(UTC))
    assert processed == 2  # planning (pending) + finished (success)
    assert ("acme", "vcs-dispatch", "prsha-disp", "success", "stackd/plan") in statuses
    assert any(state == "pending" for *_, state, _ in statuses)
    assert comments[0][0] == 9
    assert "+2 ~1 -0" in comments[-1][1]

    async with SessionLocal() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run.vcs_comment_id == 555
        assert await dispatcher.dispatch_vcs(s, datetime.now(UTC)) == 0


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
