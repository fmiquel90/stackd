from __future__ import annotations

import httpx

from tests.conftest_phase2 import login


async def test_tier_name_charset_rejected(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    # ':' would alias the OIDC sub (run:<tier>:<stack>:<phase>); whitespace likewise.
    for bad in ["prod:foo", "pre prod", "Prod"]:
        r = await client.post("/api/v1/tiers", headers=admin, json={"name": bad})
        assert r.status_code == 422, bad
    ok = await client.post("/api/v1/tiers", headers=admin, json={"name": "qa-eu"})
    assert ok.status_code == 201


async def test_update_user_rejects_unknown_tier(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    await login(client, "bob")
    users = (await client.get("/api/v1/users", headers=admin)).json()
    bob = next(u for u in users if u["email"] == "bob@dev.local")
    bad = await client.patch(
        f"/api/v1/users/{bob['id']}", headers=admin, json={"allowed_tiers": ["dev", "nope"]}
    )
    assert bad.status_code == 422


async def test_delete_tier_strips_user_grants(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    await login(client, "bob")
    # Create a throwaway tier, grant it to bob, then delete it.
    await client.post("/api/v1/tiers", headers=admin, json={"name": "qa-strip"})
    users = (await client.get("/api/v1/users", headers=admin)).json()
    bob = next(u for u in users if u["email"] == "bob@dev.local")
    granted = await client.patch(
        f"/api/v1/users/{bob['id']}", headers=admin, json={"allowed_tiers": ["dev", "qa-strip"]}
    )
    assert "qa-strip" in granted.json()["allowed_tiers"]

    tiers = (await client.get("/api/v1/tiers", headers=admin)).json()
    qa = next(t for t in tiers if t["name"] == "qa-strip")
    assert (await client.delete(f"/api/v1/tiers/{qa['id']}", headers=admin)).status_code == 204

    # The stale name must be gone from bob (no privilege resurrection if recreated later).
    users = (await client.get("/api/v1/users", headers=admin)).json()
    bob = next(u for u in users if u["email"] == "bob@dev.local")
    assert "qa-strip" not in bob["allowed_tiers"]

    # Restore the shared persona (other tests assume bob's seeded grants).
    await client.patch(
        f"/api/v1/users/{bob['id']}", headers=admin, json={"allowed_tiers": ["dev", "staging"]}
    )


async def test_delete_tier_in_use_blocked(client: httpx.AsyncClient) -> None:
    from tests.conftest_phase2 import make_env, make_stack

    admin = await login(client, "admin")
    await client.post("/api/v1/tiers", headers=admin, json={"name": "qa-used"})
    stack = await make_stack(client, admin, "qa-used-stack")
    await make_env(client, admin, stack, "e", "qa-used")
    tiers = (await client.get("/api/v1/tiers", headers=admin)).json()
    qa = next(t for t in tiers if t["name"] == "qa-used")
    blocked = await client.delete(f"/api/v1/tiers/{qa['id']}", headers=admin)
    assert blocked.status_code == 409


async def test_delete_tier_removes_variable_set_attachments(client: httpx.AsyncClient) -> None:
    """A tier's variable-set attachments go with it (audited), like a user's or a member's grant —
    re-creating the name later must not resurrect them."""
    import uuid

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.audit import AuditEvent

    admin = await login(client, "admin")
    tier = (await client.post("/api/v1/tiers", headers=admin, json={"name": "qa-vs"})).json()
    vset = await client.post("/api/v1/variable-sets", headers=admin, json={"name": "qa-vs-set"})
    assert vset.status_code == 201, vset.text
    set_id = vset.json()["id"]
    attached = await client.post(
        f"/api/v1/variable-sets/{set_id}/attachments",
        headers=admin,
        json={"target_kind": "tier", "target_id": tier["id"]},
    )
    assert attached.status_code == 201, attached.text

    assert (await client.delete(f"/api/v1/tiers/{tier['id']}", headers=admin)).status_code == 204
    left = await client.get(f"/api/v1/variable-sets/{set_id}/attachments", headers=admin)
    assert left.json() == []
    async with SessionLocal() as session:
        context = (
            await session.execute(
                select(AuditEvent.context).where(
                    AuditEvent.action == "variable_set.detached",
                    AuditEvent.target_id == uuid.UUID(set_id),
                )
            )
        ).scalar_one()
    assert context == {"target_kind": "tier", "target_id": tier["id"], "reason": "tier_deleted"}


async def test_repo_hooks_at_apply_is_off_unless_the_tier_allows_it(
    client: httpx.AsyncClient,
) -> None:
    """§8.3: the claim tells the worker whether the repo's hooks may run at the apply stages —
    a tier's flag, off by default, settable (and audited) by an admin."""
    from tests.conftest_phase2 import make_env, make_stack, register_worker

    admin = await login(client, "admin")
    tier = await client.post("/api/v1/tiers", headers=admin, json={"name": "qa-hooks"})
    assert tier.status_code == 201 and tier.json()["repo_hooks_at_apply"] is False
    wh = await register_worker(client, admin, "pool-repo-hooks")
    env_id = await make_env(
        client, admin, await make_stack(client, admin, "hooks-st"), "e", "qa-hooks"
    )

    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["repo_hooks_at_apply"] is False

    alice = await login(client, "alice")
    url = f"/api/v1/tiers/{tier.json()['id']}"
    assert (
        await client.patch(url, headers=alice, json={"repo_hooks_at_apply": True})
    ).status_code == 403
    updated = await client.patch(url, headers=admin, json={"repo_hooks_at_apply": True})
    assert updated.status_code == 200 and updated.json()["repo_hooks_at_apply"] is True
    audit = (await client.get("/api/v1/audit?action=tier.updated", headers=admin)).json()
    items = audit["items"] if isinstance(audit, dict) else audit
    assert any((e.get("context") or {}).get("values", {}).get("repo_hooks_at_apply") for e in items)


async def test_default_tiers_disallow_repo_apply_hooks(client: httpx.AsyncClient) -> None:
    """Fail closed: no tier allows them until an admin says so — the default catalog included."""
    admin = await login(client, "admin")
    tiers = {t["name"]: t for t in (await client.get("/api/v1/tiers", headers=admin)).json()}
    assert not any(tiers[n]["repo_hooks_at_apply"] for n in ("dev", "staging", "prod"))
