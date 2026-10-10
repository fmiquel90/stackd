from __future__ import annotations

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _bell(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    return (await client.get("/api/v1/notifications", headers=headers)).json()


async def _drive_to_unconfirmed(client, wh, env_id, triggerer) -> str:
    run = (
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=triggerer, json={})
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    return run["id"]


async def test_approval_request_fans_out_to_approvers(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")  # approver, prod
    await login(client, "bob")
    wh = await register_worker(client, admin, "pool-inbox-1")
    stack = await make_stack(client, admin, "inbox1-stack")
    env_id = await make_env(client, admin, stack, "prod", "prod")

    # bob (writer) triggers → unconfirmed → approvers (alice, admin) get an approval_request.
    bob = await login(client, "bob")
    await _drive_to_unconfirmed(client, wh, env_id, bob)

    alice_feed = await _bell(client, alice)
    assert any(n["kind"] == "approval_request" for n in alice_feed)
    assert all(n["read"] is False for n in alice_feed if n["kind"] == "approval_request")


async def test_approval_audience_follows_space_membership(client: httpx.AsyncClient) -> None:
    # The fan-out uses the same effective rule as the confirm gate (space membership + can_apply),
    # not instance role/tiers: an instance approver with no membership in the space is not told
    # (cross-space leak), a membership-only approver (instance writer) is.
    admin = await login(client, "admin")
    alice = await login(client, "alice")  # instance approver {dev, staging, prod}
    bob = await login(client, "bob")  # instance writer
    wh = await register_worker(client, admin, "pool-inbox-6")
    space = await client.post("/api/v1/spaces", headers=admin, json={"name": "inbox-space"})
    assert space.status_code == 201, space.text
    space_id = space.json()["id"]
    users = (await client.get("/api/v1/users", headers=admin)).json()
    ids = {u["email"]: u["id"] for u in users}
    # Pin memberships regardless of provisioning order: alice none, bob approver for prod.
    await client.delete(
        f"/api/v1/spaces/{space_id}/members/{ids['alice@dev.local']}", headers=admin
    )
    grant = await client.put(
        f"/api/v1/spaces/{space_id}/members",
        headers=admin,
        json={"user_id": ids["bob@dev.local"], "role": "approver", "allowed_tiers": ["prod"]},
    )
    assert grant.status_code == 200, grant.text
    stack = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "space_id": space_id,
            "name": "inbox-space-stack",
            "repo_url": "file:///repos/x",
            "tool_version": "1.12.0",
        },
    )
    assert stack.status_code == 201, stack.text
    env_id = await make_env(client, admin, stack.json()["id"], "prod", "prod")

    run_id = await _drive_to_unconfirmed(client, wh, env_id, admin)

    assert any(
        n["kind"] == "approval_request" and n["run_id"] == run_id for n in await _bell(client, bob)
    )
    assert all(n["run_id"] != run_id for n in await _bell(client, alice))
    # The audience matches who can actually act on it.
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=alice)).status_code == 403
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=bob)).status_code == 200


async def test_terminal_notifies_the_triggerer(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-inbox-2")
    stack = await make_stack(client, admin, "inbox2-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev", autodeploy=True)

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)  # autodeploy → confirmed
    await client.post("/worker/v1/jobs/claim", headers=wh)  # apply
    await event(client, wh, run["id"], "phase_finished")  # → finished

    feed = await _bell(client, admin)
    assert any(n["kind"] == "run_finished" and n["run_id"] == run["id"] for n in feed)


async def test_comment_reply_and_mention_notify(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")
    stack = await make_stack(client, admin, "inbox3-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    base = f"/api/v1/runs/{run['id']}/comments"

    # admin starts a thread; alice replies → admin gets a comment_reply.
    root = (await client.post(base, headers=admin, json={"body": "root"})).json()
    await client.post(base, headers=alice, json={"body": "reply", "parent_id": root["id"]})
    assert any(n["kind"] == "comment_reply" for n in await _bell(client, admin))

    # alice @mentions bob's local-part → bob gets a mention.
    await login(client, "bob")
    bob = await login(client, "bob")
    await client.post(base, headers=alice, json={"body": "ping @bob please look"})
    assert any(n["kind"] == "mention" for n in await _bell(client, bob))


async def test_feed_is_per_user_and_mark_read(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")
    wh = await register_worker(client, admin, "pool-inbox-4")
    stack = await make_stack(client, admin, "inbox4-stack")
    env_id = await make_env(client, admin, stack, "prod", "prod")
    bob = await login(client, "bob")
    await _drive_to_unconfirmed(client, wh, env_id, bob)

    # bob (the triggerer) is NOT notified of his own run's approval request.
    assert all(n["kind"] != "approval_request" for n in await _bell(client, bob))

    before = await _bell(client, alice)
    assert any(not n["read"] for n in before)
    await client.post("/api/v1/notifications/read", headers=alice, json={})
    assert all(n["read"] for n in await _bell(client, alice))


async def test_delete_and_clear_read(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")
    wh = await register_worker(client, admin, "pool-inbox-5")
    stack = await make_stack(client, admin, "inbox5-stack")
    env_id = await make_env(client, admin, stack, "prod", "prod")
    bob = await login(client, "bob")
    await _drive_to_unconfirmed(client, wh, env_id, bob)

    feed = await _bell(client, alice)
    nid = feed[0]["id"]
    # Delete a single notification (own only).
    assert (await client.delete(f"/api/v1/notifications/{nid}", headers=alice)).status_code == 204
    assert all(n["id"] != nid for n in await _bell(client, alice))
    # Another user can't delete it (404, not found for them).
    other = await _bell(client, alice)
    if other:
        assert (
            await client.delete(f"/api/v1/notifications/{other[0]['id']}", headers=bob)
        ).status_code == 404

    # Clear-all-read removes read ones, keeps unread.
    await client.post("/api/v1/notifications/read", headers=alice, json={})
    assert (await client.delete("/api/v1/notifications", headers=alice)).status_code == 204
    assert await _bell(client, alice) == []
