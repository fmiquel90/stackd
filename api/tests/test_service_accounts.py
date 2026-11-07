"""Service accounts & API tokens (SPECS §23): automation reads, plans and exports — never decides."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models.api_token import ApiToken
from app.models.audit import AuditEvent
from tests.conftest_phase2 import login, make_env, make_stack, register_worker
from tests.test_runs import _drive_to_unconfirmed


async def _account(client: httpx.AsyncClient, admin: dict, role: str = "writer") -> dict:
    r = await client.post(
        "/api/v1/service-accounts",
        headers=admin,
        json={"name": f"ci-{uuid.uuid4().hex[:8]}", "role": role},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _token(client: httpx.AsyncClient, admin: dict, account_id: str, **body) -> dict:
    r = await client.post(
        f"/api/v1/service-accounts/{account_id}/tokens",
        headers=admin,
        json={"name": "github-actions", **body},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _grant(client, admin, space_id: str, user_id: str, role: str, tiers=None) -> None:  # type: ignore[no-untyped-def]
    r = await client.put(
        f"/api/v1/spaces/{space_id}/members",
        headers=admin,
        json={
            "user_id": user_id,
            "role": role,
            "allowed_tiers": tiers or [],
            "can_destroy": True,
        },
    )
    assert r.status_code == 200, r.text


async def test_token_authenticates_and_is_shown_once(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    sa = await _account(client, admin)
    tok = await _token(client, admin, sa["id"])
    assert tok["token"].startswith("stkd_") and tok["prefix"] == tok["token"][:12]

    h = {"Authorization": f"Bearer {tok['token']}"}
    me = await client.get("/api/v1/auth/me", headers=h)
    assert me.status_code == 200 and me.json()["kind"] == "service"

    listed = (await client.get(f"/api/v1/service-accounts/{sa['id']}/tokens", headers=admin)).json()
    assert "token" not in listed[0] and listed[0]["last_used_at"] is not None
    async with SessionLocal() as s:
        row = (
            await s.execute(select(ApiToken).where(ApiToken.id == uuid.UUID(tok["id"])))
        ).scalar_one()
        assert row.token_hash != tok["token"] and tok["token"] not in row.token_hash
        events = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "api_token.created"))
        ).scalars()
        assert all(tok["token"] not in str(e.context) for e in events)


async def test_revoked_expired_and_disabled_are_refused(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    sa = await _account(client, admin)
    tok = await _token(client, admin, sa["id"])
    h = {"Authorization": f"Bearer {tok['token']}"}
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 200

    r = await client.delete(
        f"/api/v1/service-accounts/{sa['id']}/tokens/{tok['id']}", headers=admin
    )
    assert r.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 401

    tok2 = await _token(client, admin, sa["id"])
    h2 = {"Authorization": f"Bearer {tok2['token']}"}
    async with SessionLocal() as s:
        await s.execute(
            update(ApiToken)
            .where(ApiToken.id == uuid.UUID(tok2["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    assert (await client.get("/api/v1/auth/me", headers=h2)).status_code == 401

    tok3 = await _token(client, admin, sa["id"])
    r = await client.patch(f"/api/v1/users/{sa['id']}", headers=admin, json={"disabled": True})
    assert r.status_code == 200
    h3 = {"Authorization": f"Bearer {tok3['token']}"}
    assert (await client.get("/api/v1/auth/me", headers=h3)).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer stkd_nope"})
    ).status_code == 401


async def test_roles_are_capped(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    r = await client.post(
        "/api/v1/service-accounts", headers=admin, json={"name": "too-strong", "role": "admin"}
    )
    assert r.status_code == 422
    sa = await _account(client, admin)
    r = await client.patch(f"/api/v1/users/{sa['id']}", headers=admin, json={"role": "approver"})
    assert r.status_code == 422
    bob = await login(client, "bob")
    assert (await client.get("/api/v1/service-accounts", headers=bob)).status_code == 403
    assert (
        await client.post("/api/v1/service-accounts", headers=bob, json={"name": "x"})
    ).status_code == 403


async def test_plans_and_exports_but_never_confirms(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    sa = await _account(client, admin, role="writer")
    h = {"Authorization": f"Bearer {(await _token(client, admin, sa['id']))['token']}"}

    stack = await make_stack(client, admin, f"sa-stack-{uuid.uuid4().hex[:6]}")
    space_id = (await client.get(f"/api/v1/stacks/{stack}", headers=admin)).json()["space_id"]
    env_id = await make_env(client, admin, stack, "dev", "dev")

    # No membership yet: a service account reaches no space by default.
    assert (await client.get(f"/api/v1/stacks/{stack}", headers=h)).status_code == 403

    # Even as the space's admin with every tier and destroy, it can't decide.
    await _grant(client, admin, space_id, sa["id"], "admin", ["dev", "staging", "prod"])
    r = await client.post(f"/api/v1/environments/{env_id}/runs", headers=h, json={})
    assert r.status_code == 201, r.text
    assert r.json()["triggered_by"] == "manual"
    r = await client.post(
        f"/api/v1/environments/{env_id}/runs", headers=h, json={"type": "destroy"}
    )
    assert r.status_code == 403

    # Config export is a space-admin read: allowed (§22).
    assert (await client.get(f"/api/v1/spaces/{space_id}/export", headers=h)).status_code == 200

    wh = await register_worker(client, admin, f"sa-pool-{uuid.uuid4().hex[:6]}")
    async with SessionLocal() as s:
        from sqlalchemy import text

        await s.execute(text("DELETE FROM runs WHERE environment_id = :e"), {"e": env_id})
        await s.commit()
    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)
    refused = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=h)
    assert refused.status_code == 403
    assert "service accounts" in refused.text
    # A mutating command is an apply-level act: refused too.
    r = await client.post(
        f"/api/v1/environments/{env_id}/commands",
        headers=h,
        json={"command": "taint", "args": ["a.b"]},
    )
    assert r.status_code in (403, 409)


async def test_bootstrap_admin_emails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """§2.1: with STACKD_BOOTSTRAP_ADMIN_EMAILS set, only the listed emails become admin on first
    sign-in — there is no first-come admin seat."""
    from types import SimpleNamespace

    from app.auth import provisioning
    from app.enums import Role

    settings = SimpleNamespace(bootstrap_admin_emails={"ops@corp.example"})
    monkeypatch.setattr(provisioning, "get_settings", lambda: settings)
    async with SessionLocal() as s:
        stranger = await provisioning.upsert_user(
            s, google_sub=f"g-{uuid.uuid4()}", email="someone@corp.example"
        )
        ops = await provisioning.upsert_user(
            s, google_sub=f"g-{uuid.uuid4()}", email="Ops@corp.example"
        )
        assert stranger.role == Role.reader and ops.role == Role.admin
        await s.rollback()


async def test_no_inbox_no_mention_and_a_unique_name(client: httpx.AsyncClient) -> None:
    """A service account is no @mention target and gets no inbox row; its name is unique at the
    database level (a concurrent create can't slip past the check)."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.enums import Role, UserKind
    from app.inbox.service import notify
    from app.models.user import User
    from app.models.user_notification import UserNotification

    admin = await login(client, "admin")
    sa = await _account(client, admin)
    mentionable = (await client.get("/api/v1/users/mentionable", headers=admin)).json()
    assert sa["id"] not in {u["id"] for u in mentionable}

    async with SessionLocal() as s:
        await notify(s, uuid.UUID(sa["id"]), "run_finished")
        await s.commit()
        rows = (
            (
                await s.execute(
                    select(UserNotification).where(UserNotification.user_id == uuid.UUID(sa["id"]))
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    async with SessionLocal() as s:
        s.add(User(kind=UserKind.service, email=sa["email"], display_name="dup", role=Role.reader))
        with pytest.raises(IntegrityError):
            await s.flush()
