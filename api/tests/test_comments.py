from __future__ import annotations

import httpx

from tests.conftest_phase2 import login, make_env, make_stack


async def _make_run(client: httpx.AsyncClient, admin: dict, name: str) -> str:
    stack = await make_stack(client, admin, f"cmt-{name}")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    return run.json()["id"]


async def test_comment_thread_and_anchor(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    run_id = await _make_run(client, admin, "THREAD")
    base = f"/api/v1/runs/{run_id}/comments"

    # General comment.
    g = await client.post(base, headers=admin, json={"body": "Looks fine overall"})
    assert g.status_code == 201
    assert g.json()["anchor"] is None and g.json()["resolved"] is False

    # Anchored comment (plan_line) + a reply.
    a = await client.post(
        base,
        headers=admin,
        json={
            "body": "Why is this bucket being replaced?",
            "anchor": {
                "kind": "plan_line",
                "phase": "planning",
                "seq": 2,
                "line_start": 10,
                "line_end": 12,
                "snippet": "-/+ aws_s3_bucket.logs",
            },
        },
    )
    assert a.status_code == 201
    anchor = a.json()["anchor"]
    assert anchor["kind"] == "plan_line" and anchor["seq"] == 2
    parent_id = a.json()["id"]

    reply = await client.post(
        base, headers=admin, json={"body": "force_destroy changed", "parent_id": parent_id}
    )
    assert reply.status_code == 201 and reply.json()["parent_id"] == parent_id

    listing = (await client.get(base, headers=admin)).json()
    assert len(listing) == 3


async def test_anchor_validation(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    run_id = await _make_run(client, admin, "ANCHOR")
    bad = await client.post(
        f"/api/v1/runs/{run_id}/comments",
        headers=admin,
        json={"body": "x", "anchor": {"kind": "plan_line"}},  # missing phase/seq
    )
    assert bad.status_code == 422


async def test_no_nested_replies(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    run_id = await _make_run(client, admin, "NEST")
    base = f"/api/v1/runs/{run_id}/comments"
    root = (await client.post(base, headers=admin, json={"body": "root"})).json()
    reply = (
        await client.post(base, headers=admin, json={"body": "r1", "parent_id": root["id"]})
    ).json()
    nested = await client.post(base, headers=admin, json={"body": "r2", "parent_id": reply["id"]})
    assert nested.status_code == 422  # threads stay one level deep


async def test_resolve_and_edit_permissions(client: httpx.AsyncClient) -> None:
    alice = await login(client, "alice")  # approver
    bob = await login(client, "bob")  # writer
    admin = await login(client, "admin")
    run_id = await _make_run(client, admin, "PERM")
    base = f"/api/v1/runs/{run_id}/comments"

    c = await client.post(base, headers=bob, json={"body": "question?"})
    cid = c.json()["id"]

    # Non-author can't edit the body.
    refused = await client.patch(f"{base}/{cid}", headers=alice, json={"body": "hijack"})
    assert refused.status_code == 403

    # An approver (not the author) may resolve the thread.
    resolved = await client.patch(f"{base}/{cid}", headers=alice, json={"resolved": True})
    assert resolved.status_code == 200 and resolved.json()["resolved"] is True

    # Author edits their own body.
    edited = await client.patch(f"{base}/{cid}", headers=bob, json={"body": "clarified"})
    assert edited.status_code == 200 and edited.json()["body"] == "clarified"


async def test_delete_only_author_or_admin(client: httpx.AsyncClient) -> None:
    bob = await login(client, "bob")
    alice = await login(client, "alice")
    admin = await login(client, "admin")
    run_id = await _make_run(client, admin, "DEL")
    base = f"/api/v1/runs/{run_id}/comments"

    cid = (await client.post(base, headers=bob, json={"body": "mine"})).json()["id"]
    assert (
        await client.delete(f"{base}/{cid}", headers=alice)
    ).status_code == 403  # approver ≠ author
    assert (await client.delete(f"{base}/{cid}", headers=admin)).status_code == 204  # admin can


async def test_delete_thread_with_replies_guarded(client: httpx.AsyncClient) -> None:
    bob = await login(client, "bob")
    alice = await login(client, "alice")
    admin = await login(client, "admin")
    run_id = await _make_run(client, admin, "DELREP")
    base = f"/api/v1/runs/{run_id}/comments"

    root = (await client.post(base, headers=bob, json={"body": "root"})).json()["id"]
    await client.post(base, headers=alice, json={"body": "reply", "parent_id": root})
    # The author can't nuke a thread carrying someone else's reply (cascade) …
    assert (await client.delete(f"{base}/{root}", headers=bob)).status_code == 409
    # … but an admin can.
    assert (await client.delete(f"{base}/{root}", headers=admin)).status_code == 204
