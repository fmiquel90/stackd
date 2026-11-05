from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid

import httpx

from tests.conftest_phase2 import event, login, make_env, register_worker

REPO = "https://github.com/acme/demo"
SECRET = "whsecret123"


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _stack_with_webhook(client, admin, name: str, repo: str = REPO) -> tuple[str, str]:
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"name": name, "repo_url": repo, "tool_version": "1.12.0"},
    )
    stack_id = r.json()["id"]
    await client.patch(f"/api/v1/stacks/{stack_id}", headers=admin, json={"webhook_secret": SECRET})
    env_id = await make_env(client, admin, stack_id, "main", "dev")
    return stack_id, env_id


async def test_push_updates_head_and_triggers(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-push")

    payload = {
        "ref": "refs/heads/main",
        "after": "abc1234def",
        "repository": {"clone_url": REPO + ".git", "html_url": REPO},
        "commits": [{"modified": ["main.tf"]}],
    }
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sig(body),
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["triggered"] == 1

    env = (await client.get(f"/api/v1/environments/{env_id}", headers=admin)).json()
    assert env["head_sha"] == "abc1234def"
    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    assert runs[0]["triggered_by"] == "webhook"
    assert runs[0]["commit_sha"] == "abc1234def"


async def test_invalid_signature_rejected(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    await _stack_with_webhook(client, admin, "wh-bad")
    payload = {"ref": "refs/heads/main", "after": "x", "repository": {"clone_url": REPO}}
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


async def test_pull_request_creates_proposed_run(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-pr")
    payload = {
        "action": "opened",
        "repository": {"clone_url": REPO},
        "pull_request": {"base": {"ref": "main"}, "head": {"sha": "prsha999"}},
    }
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sig(body),
            "X-GitHub-Delivery": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 200
    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    assert any(r["type"] == "proposed" and r["commit_sha"] == "prsha999" for r in runs)


async def test_branch_deletion_push_clears_head_and_triggers_nothing(
    client: httpx.AsyncClient,
) -> None:
    """A push deleting the tracked branch (`deleted`, all-zero `after`) triggers no run and records
    no zero SHA: the env's head is cleared, so it isn't reported stale against a gone branch."""
    admin = await login(client, "admin")
    repo = "https://github.com/acme/branch-deleted"  # its own repo: only this env matches
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-branch-deleted", repo)

    async def _push(payload: dict) -> httpx.Response:
        body = json.dumps(
            {"ref": "refs/heads/main", "repository": {"clone_url": repo}} | payload
        ).encode()
        return await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sig(body),
                "X-GitHub-Delivery": str(uuid.uuid4()),
            },
        )

    assert (await _push({"after": "abc1234def", "commits": [{"modified": ["main.tf"]}]})).json()[
        "triggered"
    ] == 1
    runs_before = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()

    zero = "0" * 40
    deleted = await _push({"before": "abc1234def", "after": zero, "deleted": True, "commits": []})
    assert deleted.status_code == 200
    assert deleted.json()["triggered"] == 0

    env = (await client.get(f"/api/v1/environments/{env_id}", headers=admin)).json()
    assert env["head_sha"] is None
    assert env["stale"] is False
    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    assert [r["id"] for r in runs] == [r["id"] for r in runs_before]
    assert all(r["commit_sha"] != zero for r in runs)


async def test_push_skips_an_env_re_pointed_meanwhile(client: httpx.AsyncClient) -> None:
    """The head is recorded on the envs still tracking the pushed branch when it is written: an env
    re-pointed to another branch by a transaction the push has to wait for gets neither the pushed
    head nor a run on it."""
    from sqlalchemy import text, update

    from app.db import SessionLocal
    from app.models.environment import Environment

    admin = await login(client, "admin")
    repo = "https://github.com/acme/re-pointed"  # its own repo: only this env matches
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-re-pointed", repo)
    payload = {
        "ref": "refs/heads/main",
        "after": "pushed999",
        "repository": {"clone_url": repo},
        "commits": [{"modified": ["main.tf"]}],
    }
    body = json.dumps(payload).encode()

    async def _lock_waiters() -> int:
        async with SessionLocal() as session:
            return (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()

    async with SessionLocal() as patch:  # a branch change in flight, as PATCH does it
        await patch.execute(
            update(Environment)
            .where(Environment.id == uuid.UUID(env_id))
            .values(branch="other", head_sha=None, head_updated_at=None)
        )
        push = asyncio.create_task(
            client.post(
                "/api/v1/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": _sig(body),
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                },
            )
        )
        for _ in range(200):  # until the push waits on the env's row lock (or is done)
            if push.done() or await _lock_waiters():
                break
            await asyncio.sleep(0.025)
        await patch.commit()
    resp = await push

    assert resp.status_code == 200
    assert resp.json()["triggered"] == 0
    env = (await client.get(f"/api/v1/environments/{env_id}", headers=admin)).json()
    assert (env["branch"], env["head_sha"]) == ("other", None)
    assert (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json() == []


# --- anti-replay (SPECS §13) ---


async def _deliver(
    client: httpx.AsyncClient,
    payload: dict,
    event: str,
    delivery: str | None,
    sig: str | None = None,
) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": sig or _sig(body)}
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return await client.post("/api/v1/webhooks/github", content=body, headers=headers)


async def _backdate(delivery: str, minutes: int) -> None:
    """Pretend `delivery` was first received `minutes` ago."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.webhook_delivery import WebhookDelivery

    async with SessionLocal() as session:
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.delivery_id == delivery)
            .values(received_at=datetime.now(UTC) - timedelta(minutes=minutes))
        )
        await session.commit()


async def test_a_replayed_delivery_is_a_no_op(client: httpx.AsyncClient) -> None:
    """The same `X-GitHub-Delivery` id within 5 min is acknowledged (2xx, `duplicate`) and not
    acted on again: one run, whatever GitHub (or a replayer) sends twice."""
    admin = await login(client, "admin")
    repo = "https://github.com/acme/replay"
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-replay", repo)
    payload = {
        "ref": "refs/heads/main",
        "after": "abc1234def",
        "repository": {"clone_url": repo},
        "commits": [{"modified": ["main.tf"]}],
    }
    delivery = str(uuid.uuid4())

    first = await _deliver(client, payload, "push", delivery)
    assert first.status_code == 200 and first.json()["triggered"] == 1
    again = await _deliver(client, payload, "push", delivery)
    assert again.status_code == 200
    assert again.json() == {"matched": 1, "triggered": 0, "duplicate": True}
    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    assert len(runs) == 1


async def test_a_delivery_past_the_window_is_processed_again(client: httpx.AsyncClient) -> None:
    """Past the replay window the id counts as new (e.g. a deliberate Redeliver from GitHub)."""
    admin = await login(client, "admin")
    repo = "https://github.com/acme/replay-window"
    await _stack_with_webhook(client, admin, "wh-replay-window", repo)
    ping = {"zen": "hi", "repository": {"clone_url": repo}}
    delivery = str(uuid.uuid4())

    assert "duplicate" not in (await _deliver(client, ping, "ping", delivery)).json()
    assert (await _deliver(client, ping, "ping", delivery)).json()["duplicate"] is True
    await _backdate(delivery, minutes=4)
    assert (await _deliver(client, ping, "ping", delivery)).json()["duplicate"] is True
    await _backdate(delivery, minutes=6)
    assert "duplicate" not in (await _deliver(client, ping, "ping", delivery)).json()
    assert (await _deliver(client, ping, "ping", delivery)).json()["duplicate"] is True


async def test_a_delivery_without_an_id_is_rejected(client: httpx.AsyncClient) -> None:
    """An authenticated request without `X-GitHub-Delivery` (GitHub always sends one) → 400, and
    nothing runs; a bad signature stays a 401, and doesn't record its delivery id."""
    admin = await login(client, "admin")
    repo = "https://github.com/acme/replay-noid"
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-replay-noid", repo)
    payload = {"ref": "refs/heads/main", "after": "abc", "repository": {"clone_url": repo}}

    missing = await _deliver(client, payload, "push", None)
    assert missing.status_code == 400
    assert (await _deliver(client, payload, "push", "x" * 200)).status_code == 400
    delivery = str(uuid.uuid4())
    forged = await _deliver(client, payload, "push", delivery, sig="sha256=deadbeef")
    assert forged.status_code == 401
    assert (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json() == []
    # The forged attempt did not burn the id: the genuine delivery is processed.
    assert (await _deliver(client, payload, "push", delivery)).json()["triggered"] == 1


async def test_retention_purges_delivery_ids_past_the_window(client: httpx.AsyncClient) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.webhook_delivery import WebhookDelivery
    from app.webhooks.replay import purge_webhook_deliveries

    admin = await login(client, "admin")
    repo = "https://github.com/acme/replay-purge"
    await _stack_with_webhook(client, admin, "wh-replay-purge", repo)
    old, fresh = str(uuid.uuid4()), str(uuid.uuid4())
    for delivery in (old, fresh):
        await _deliver(client, {"repository": {"clone_url": repo}}, "ping", delivery)
    await _backdate(old, minutes=10)

    async with SessionLocal() as session:
        assert await purge_webhook_deliveries(session, datetime.now(UTC)) >= 1
        left = set(
            (
                await session.execute(
                    select(WebhookDelivery.delivery_id).where(
                        WebhookDelivery.delivery_id.in_((old, fresh))
                    )
                )
            ).scalars()
        )
    assert left == {fresh}


PR_REPO = "https://github.com/acme/pr-close"  # its own repo: no other test's stack matches it


async def _pull_request(client, action: str, number: int, sha: str) -> httpx.Response:  # type: ignore[no-untyped-def]
    payload = {
        "action": action,
        "number": number,
        "repository": {"clone_url": PR_REPO},
        "pull_request": {"number": number, "base": {"ref": "main"}, "head": {"sha": sha}},
    }
    body = json.dumps(payload).encode()
    return await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": _sig(body),
        },
    )


async def test_closing_a_pr_stops_its_executing_runs_through_the_worker(
    client: httpx.AsyncClient,
) -> None:
    # A proposed run a worker is planning is stopped like a user's stop (#29, §4.1): a request
    # the worker honours, never a `canceled` under its feet. A run not claimed yet is canceled.
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.audit import AuditEvent

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-wh-pr-close")
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-pr-close", repo=PR_REPO)

    assert (await _pull_request(client, "opened", 71, "a" * 40)).status_code == 200
    executing = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["job_id"]
    await event(client, wh, executing, "phase_started", phase="planning")
    assert (await _pull_request(client, "opened", 72, "b" * 40)).status_code == 200
    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    queued = next(r["id"] for r in runs if r["id"] != executing)  # waits: the env is busy

    assert (await _pull_request(client, "closed", 71, "a" * 40)).status_code == 200
    run = (await client.get(f"/api/v1/runs/{executing}", headers=admin)).json()
    assert run["state"] == "planning" and run["cancel_requested_at"] is not None
    beat = (await client.post("/worker/v1/heartbeat", headers=wh, json={"in_flight": 1})).json()
    assert [c["payload"] for c in beat["commands"] if c["type"] == "cancel_job"] == [
        {"job_id": executing}
    ]
    assert (await event(client, wh, executing, "job_canceled", phase="planning")).status_code == 200
    assert (await client.get(f"/api/v1/runs/{executing}", headers=admin)).json()[
        "state"
    ] == "canceled"
    async with SessionLocal() as s:
        requested = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "run.cancel_requested",
                    AuditEvent.target_id == uuid.UUID(executing),
                )
            )
        ).scalar_one()
    assert requested.actor_kind == "system" and requested.context["reason"] == "pr_closed"

    assert (await _pull_request(client, "closed", 72, "b" * 40)).status_code == 200
    assert (await client.get(f"/api/v1/runs/{queued}", headers=admin)).json()["state"] == "canceled"
