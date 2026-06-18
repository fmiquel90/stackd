from __future__ import annotations

import httpx

from tests.conftest_phase2 import login


async def _user_id(client: httpx.AsyncClient, admin: dict[str, str], email: str) -> str:
    users = (await client.get("/api/v1/users", headers=admin)).json()
    return next(u["id"] for u in users if u["email"] == email)


async def _create_space(client: httpx.AsyncClient, admin: dict[str, str], name: str) -> str:
    r = await client.post("/api/v1/spaces", headers=admin, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _stack_in(client: httpx.AsyncClient, h: dict[str, str], space_id: str, name: str) -> str:
    r = await client.post(
        "/api/v1/stacks",
        headers=h,
        json={
            "space_id": space_id,
            "name": name,
            "repo_url": "file:///repos/x",
            "tool_version": "1.12.0",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_non_member_cannot_see_or_get_other_space(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # provisioned into the seed spaces, NOT into teamx
    space_id = await _create_space(client, admin, "teamx")
    stack_id = await _stack_in(client, admin, space_id, "teamx-stack")

    # bob is not a member of teamx → the stack is invisible and a direct GET is forbidden.
    listed = (await client.get("/api/v1/stacks", headers=bob)).json()
    assert all(s["id"] != stack_id for s in listed)
    assert (await client.get(f"/api/v1/stacks/{stack_id}", headers=bob)).status_code == 403

    # admin (instance admin) sees every space's resources.
    admin_list = (await client.get("/api/v1/stacks", headers=admin)).json()
    assert any(s["id"] == stack_id for s in admin_list)


async def test_membership_grants_then_revokes_access(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")
    space_id = await _create_space(client, admin, "teamy")
    stack_id = await _stack_in(client, admin, space_id, "teamy-stack")
    bob_id = await _user_id(client, admin, "bob@dev.local")

    # Grant bob reader in teamy → he can now read the stack.
    grant = await client.put(
        f"/api/v1/spaces/{space_id}/members",
        headers=admin,
        json={"user_id": bob_id, "role": "reader", "allowed_tiers": []},
    )
    assert grant.status_code == 200, grant.text
    assert (await client.get(f"/api/v1/stacks/{stack_id}", headers=bob)).status_code == 200
    # But a reader cannot mutate.
    assert (
        await client.patch(f"/api/v1/stacks/{stack_id}", headers=bob, json={"description": "nope"})
    ).status_code == 403

    # Revoke → access gone again.
    rm = await client.delete(f"/api/v1/spaces/{space_id}/members/{bob_id}", headers=admin)
    assert rm.status_code == 204
    assert (await client.get(f"/api/v1/stacks/{stack_id}", headers=bob)).status_code == 403


async def test_list_spaces_is_membership_scoped(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")
    secret_space = await _create_space(client, admin, "secret")

    bob_spaces = {s["id"] for s in (await client.get("/api/v1/spaces", headers=bob)).json()}
    assert secret_space not in bob_spaces  # bob was never added
    admin_spaces = {s["id"] for s in (await client.get("/api/v1/spaces", headers=admin)).json()}
    assert secret_space in admin_spaces  # instance admin sees all


async def test_create_space_requires_instance_admin(client: httpx.AsyncClient) -> None:
    bob = await login(client, "bob")
    assert (await client.post("/api/v1/spaces", headers=bob, json={"name": "z"})).status_code == 403


async def test_members_listing_requires_space_admin(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")
    space_id = await _create_space(client, admin, "teamz")
    # bob is not a member at all → forbidden.
    assert (await client.get(f"/api/v1/spaces/{space_id}/members", headers=bob)).status_code == 403
    # admin can list — the creator is auto-enrolled as a space admin.
    members = (await client.get(f"/api/v1/spaces/{space_id}/members", headers=admin)).json()
    assert any(m["email"] == "admin@dev.local" and m["role"] == "admin" for m in members)
