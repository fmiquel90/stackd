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
