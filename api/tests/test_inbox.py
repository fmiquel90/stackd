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
