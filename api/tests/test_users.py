from __future__ import annotations

import httpx


async def _login(client: httpx.AsyncClient, persona: str) -> str:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": persona})
    return r.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_non_admin_cannot_list_users(client: httpx.AsyncClient) -> None:
    token = await _login(client, "bob")  # writer
    r = await client.get("/api/v1/users", headers=_bearer(token))
    assert r.status_code == 403


async def test_mentionable_directory_readable_by_any_user(client: httpx.AsyncClient) -> None:
    bob = await _login(client, "bob")  # writer — not admin
    await _login(client, "alice")  # ensure another user exists
    r = await client.get("/api/v1/users/mentionable", headers=_bearer(bob))
    assert r.status_code == 200
    rows = r.json()
    assert any(u["email"] == "alice@dev.local" for u in rows)
    # Minimal directory — no roles/permissions leaked.
    assert set(rows[0].keys()) == {"id", "email", "display_name"}


async def test_admin_updates_tier_is_audited(client: httpx.AsyncClient) -> None:
    admin_token = await _login(client, "admin")
    await _login(client, "bob")  # ensure bob exists

    users = (await client.get("/api/v1/users", headers=_bearer(admin_token))).json()
    bob = next(u for u in users if u["email"] == "bob@dev.local")
    assert bob["allowed_tiers"] == ["dev", "staging"]

    patched = await client.patch(
        f"/api/v1/users/{bob['id']}",
        headers=_bearer(admin_token),
        # Non-contiguous set — impossible under the old linear ceiling.
        json={"allowed_tiers": ["dev", "prod"], "can_destroy": True},
    )
    assert patched.status_code == 200
    assert patched.json()["allowed_tiers"] == ["dev", "prod"]
    assert patched.json()["can_destroy"] is True

    # Invariant #2: the mutation wrote audit events in the same txn (/audit HTTP is Phase 3).
    import uuid

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.audit import AuditEvent

    async with SessionLocal() as session:
        actions = set(
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.target_id == uuid.UUID(bob["id"]))
                )
            )
            .scalars()
            .all()
        )
    assert "user.apply_tier_changed" in actions
    assert "user.destroy_permission_changed" in actions
