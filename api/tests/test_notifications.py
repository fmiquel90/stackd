from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _drive_to_unconfirmed(client, wh, env_id, admin) -> str:
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    return run["id"]


async def test_notification_target_crud(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "notif-crud")

    created = await client.post(
        f"/api/v1/stacks/{stack}/notifications",
        headers=admin,
        json={"name": "ops-slack", "kind": "slack", "url": "https://hooks.example/x"},
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    assert created.json()["on_states"] == ["unconfirmed", "failed"]  # default

    listed = await client.get(f"/api/v1/stacks/{stack}/notifications", headers=admin)
    assert [t["id"] for t in listed.json()] == [tid]

    patched = await client.patch(
        f"/api/v1/stacks/{stack}/notifications/{tid}", headers=admin, json={"enabled": False}
    )
    assert patched.json()["enabled"] is False

    deleted = await client.delete(f"/api/v1/stacks/{stack}/notifications/{tid}", headers=admin)
    assert deleted.status_code == 204


async def test_send_test_notification(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.notifications import router as notif_router

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "notif-test")
    tid = (
        await client.post(
            f"/api/v1/stacks/{stack}/notifications",
            headers=admin,
            json={"name": "ops", "kind": "slack", "url": "https://hooks.example/x"},
        )
    ).json()["id"]

    captured: list[dict] = []

    async def fake_deliver(target, body):  # type: ignore[no-untyped-def]
        captured.append(body)
        return True

    monkeypatch.setattr(notif_router, "deliver", fake_deliver)
    r = await client.post(f"/api/v1/stacks/{stack}/notifications/{tid}/test", headers=admin)
    assert r.status_code == 200 and r.json()["ok"] is True
    # The payload is explicitly flagged as a test (slack → "test" in the text).
    assert "test" in captured[0]["text"].lower()

    audit = await client.get(
        "/api/v1/audit", headers=admin, params={"target_kind": "notification_target"}
    )
    actions = {e["action"] for e in audit.json()}
    assert {
        "notification_target.created",
        "notification_target.updated",
        "notification_target.deleted",
    } <= actions


async def test_rejects_unsupported_state(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "notif-badstate")
    r = await client.post(
        f"/api/v1/stacks/{stack}/notifications",
        headers=admin,
        json={"name": "x", "kind": "webhook", "url": "https://e/x", "on_states": ["planning"]},
    )
    assert r.status_code == 422


async def test_outbox_enqueued_on_transition(client: httpx.AsyncClient) -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.notification import NotificationOutbox

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "notif-pool-1")
    stack = await make_stack(client, admin, "notif-enqueue")
    env = await make_env(client, admin, stack, "dev", "dev")

    run_id = await _drive_to_unconfirmed(client, wh, env, admin)

    async with SessionLocal() as s:
        import uuid

        rows = (
            (
                await s.execute(
                    select(NotificationOutbox).where(NotificationOutbox.run_id == uuid.UUID(run_id))
                )
            )
            .scalars()
            .all()
        )
    assert [r.to_state for r in rows] == ["unconfirmed"]


async def test_dispatcher_delivers_to_matching_targets(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import SessionLocal
    from app.notifications import dispatcher

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "notif-pool-2")
    stack = await make_stack(client, admin, "notif-dispatch")
    env = await make_env(client, admin, stack, "dev", "dev")

    # One target fires on unconfirmed (should deliver), one only on failed (should NOT).
    fire = await client.post(
        f"/api/v1/environments/{env}/notifications",
        headers=admin,
        json={
            "name": "fires",
            "kind": "webhook",
            "url": "https://e/fire",
            "on_states": ["unconfirmed"],
        },
    )
    assert fire.status_code == 201
    await client.post(
        f"/api/v1/environments/{env}/notifications",
        headers=admin,
        json={"name": "quiet", "kind": "slack", "url": "https://e/quiet", "on_states": ["failed"]},
    )

    captured: list[tuple[str, dict]] = []

    async def fake_deliver(target, body):  # type: ignore[no-untyped-def]
        captured.append((target.url, body))
        return True

    monkeypatch.setattr(dispatcher, "deliver", fake_deliver)

    run_id = await _drive_to_unconfirmed(client, wh, env, admin)

    async with SessionLocal() as s:
        processed = await dispatcher.dispatch_pending(s, datetime.now(UTC))
    assert processed == 1
    assert [url for url, _ in captured] == ["https://e/fire"]
    body = captured[0][1]
    assert body["state"] == "unconfirmed"
    assert body["run_id"] == run_id
    assert body["environment"] == "dev"

    # Second drain is a no-op: the row is marked sent.
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_pending(s, datetime.now(UTC)) == 0
    assert len(captured) == 1
