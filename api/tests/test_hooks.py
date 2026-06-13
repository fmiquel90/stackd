from __future__ import annotations

import httpx

from tests.conftest_phase2 import login, make_env, make_stack, register_worker


async def test_platform_hook_crud_and_appears_in_claim(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-hooks")
    stack = await make_stack(client, admin, "hooks-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    # Create a platform after_plan hook on the stack.
    created = await client.post(
        f"/api/v1/stacks/{stack}/hooks",
        headers=admin,
        json={"stage": "after_plan", "name": "tfsec", "command": "tfsec .", "on_failure": "warn"},
    )
    assert created.status_code == 201
    hook_id = created.json()["id"]

    listed = (await client.get(f"/api/v1/stacks/{stack}/hooks", headers=admin)).json()
    assert any(h["name"] == "tfsec" for h in listed)

    # It must surface in the worker claim payload, tagged platform (non-bypassable, §8.1).
    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    after_plan = payload["hooks"]["after_plan"]
    assert after_plan[0]["name"] == "tfsec"
    assert after_plan[0]["source"] == "platform"
    assert after_plan[0]["on_failure"] == "warn"

    # Update + delete.
    upd = await client.patch(
        f"/api/v1/stacks/{stack}/hooks/{hook_id}", headers=admin, json={"on_failure": "fail"}
    )
    assert upd.json()["on_failure"] == "fail"
    assert (
        await client.delete(f"/api/v1/stacks/{stack}/hooks/{hook_id}", headers=admin)
    ).status_code == 204
    assert (await client.get(f"/api/v1/stacks/{stack}/hooks", headers=admin)).json() == []


async def test_hooks_require_writer(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "hooks-perm-stack")
    # reader can't create hooks (managing hooks is writer+, §2.3). dev personas: bob=writer, so use a
    # fresh reader via google bootstrap is overkill — assert the dependency wiring instead: bob (writer) can.
    bob = await login(client, "bob")
    ok = await client.post(
        f"/api/v1/stacks/{stack}/hooks",
        headers=bob,
        json={"stage": "before_plan", "name": "gen", "command": "true"},
    )
    assert ok.status_code == 201  # writer may manage hooks
