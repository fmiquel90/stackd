"""Deleting a stack or an environment (§3.2, §3.4): refused while one of its environments has an
active run — the cascade would delete that run outside `transition()` — and the variable-set
attachments targeting what is deleted go with it, in the same transaction."""

from __future__ import annotations

import uuid

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def _active_run(
    client: httpx.AsyncClient, admin: dict[str, str], wh: dict[str, str], env_id: str
) -> str:
    """A run claimed by a worker (`preparing`): active, executing on that worker."""
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert job["job_id"] == run["id"]
    return run["id"]


async def _end(
    client: httpx.AsyncClient, admin: dict[str, str], wh: dict[str, str], run_id: str
) -> None:
    """The worker reports the job failed: the run turns terminal."""
    await event(client, wh, run_id, "job_failed", phase="plan")
    detail = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert detail["state"] == "failed"


async def test_deletion_refused_while_a_run_is_active(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-deletion-active")
    stack = await make_stack(client, admin, f"del-active-{uuid.uuid4().hex[:8]}")
    busy = await make_env(client, admin, stack, "busy", "dev")
    idle = await make_env(client, admin, stack, "idle", "dev")
    run_id = await _active_run(client, admin, wh, busy)

    for url in (f"/api/v1/stacks/{stack}", f"/api/v1/environments/{busy}"):
        refused = await client.delete(url, headers=admin)
        assert refused.status_code == 409, url
        assert refused.headers["content-type"].startswith("application/problem+json")
        assert refused.json()["title"] == "Active run"
        assert refused.json()["run_id"] == run_id
    assert (await client.get(f"/api/v1/stacks/{stack}", headers=admin)).status_code == 200
    detail = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert detail["state"] == "preparing"  # untouched, still the worker's

    # A sibling env with no active run is deletable meanwhile.
    assert (await client.delete(f"/api/v1/environments/{idle}", headers=admin)).status_code == 204

    # Once the run is terminal, the stack goes, its env and runs with it.
    await _end(client, admin, wh, run_id)
    assert (await client.delete(f"/api/v1/stacks/{stack}", headers=admin)).status_code == 204
    assert (await client.get(f"/api/v1/stacks/{stack}", headers=admin)).status_code == 404
    assert (await client.get(f"/api/v1/environments/{busy}", headers=admin)).status_code == 404


async def test_env_deletion_allowed_once_its_run_is_terminal(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-deletion-env")
    stack = await make_stack(client, admin, f"del-env-{uuid.uuid4().hex[:8]}")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id = await _active_run(client, admin, wh, env_id)

    assert (await client.delete(f"/api/v1/environments/{env_id}", headers=admin)).status_code == 409
    await _end(client, admin, wh, run_id)
    assert (await client.delete(f"/api/v1/environments/{env_id}", headers=admin)).status_code == 204
    assert (await client.get(f"/api/v1/environments/{env_id}", headers=admin)).status_code == 404


async def test_deletion_leaves_no_orphan_attachments(client: httpx.AsyncClient) -> None:
    """Attachments are polymorphic, with no FK (§3.4): deleting an env removes those targeting it,
    deleting a stack those targeting it and its envs — each audited as a detachment — and leaves
    the other targets' alone."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.variable_set import VariableSetAttachment

    admin = await login(client, "admin")
    suffix = uuid.uuid4().hex[:8]
    stack = await make_stack(client, admin, f"del-attach-{suffix}")
    env_a = await make_env(client, admin, stack, "a", "dev")
    env_b = await make_env(client, admin, stack, "b", "dev")
    other_stack = await make_stack(client, admin, f"del-attach-other-{suffix}")
    other_env = await make_env(client, admin, other_stack, "o", "dev")
    vset = (
        await client.post("/api/v1/variable-sets", headers=admin, json={"name": f"del-{suffix}"})
    ).json()["id"]
    url = f"/api/v1/variable-sets/{vset}/attachments"
    for kind, target in (
        ("stack", stack),
        ("environment", env_a),
        ("environment", env_b),
        ("stack", other_stack),
        ("environment", other_env),
    ):
        r = await client.post(url, headers=admin, json={"target_kind": kind, "target_id": target})
        assert r.status_code == 201, r.text

    async def _targets() -> set[str]:
        return {a["target_id"] for a in (await client.get(url, headers=admin)).json()}

    assert (await client.delete(f"/api/v1/environments/{env_a}", headers=admin)).status_code == 204
    assert await _targets() == {stack, env_b, other_stack, other_env}

    assert (await client.delete(f"/api/v1/stacks/{stack}", headers=admin)).status_code == 204
    assert await _targets() == {other_stack, other_env}
    async with SessionLocal() as session:
        orphans = (
            await session.execute(
                select(VariableSetAttachment.id).where(
                    VariableSetAttachment.target_id.in_(
                        [uuid.UUID(t) for t in (stack, env_a, env_b)]
                    )
                )
            )
        ).all()
    assert orphans == []

    events = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"action": "variable_set.detached", "target_id": vset},
        )
    ).json()
    detached = {(e["context"]["target_id"], e["context"]["reason"]) for e in events}
    assert detached == {
        (env_a, "environment_deleted"),
        (env_b, "stack_deleted"),
        (stack, "stack_deleted"),
    }


async def test_deletion_leaves_no_orphan_hooks_or_notification_targets(
    client: httpx.AsyncClient,
) -> None:
    """Platform hooks and notification targets point at their stack/env with no FK either: they go
    with the env or stack they target, each audited as a deletion, and other targets' stay."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.hook import Hook
    from app.models.notification import NotificationTarget

    admin = await login(client, "admin")
    suffix = uuid.uuid4().hex[:8]
    stack = await make_stack(client, admin, f"del-refs-{suffix}")
    env_a = await make_env(client, admin, stack, "a", "dev")
    env_b = await make_env(client, admin, stack, "b", "dev")
    other_stack = await make_stack(client, admin, f"del-refs-other-{suffix}")
    other_env = await make_env(client, admin, other_stack, "o", "dev")
    targets = {
        stack: f"/api/v1/stacks/{stack}",
        env_a: f"/api/v1/environments/{env_a}",
        env_b: f"/api/v1/environments/{env_b}",
        other_stack: f"/api/v1/stacks/{other_stack}",
        other_env: f"/api/v1/environments/{other_env}",
    }
    hooks: dict[str, str] = {}
    notifications: dict[str, str] = {}
    for target, base in targets.items():
        hook = await client.post(
            f"{base}/hooks",
            headers=admin,
            json={"stage": "after_plan", "name": f"h-{target}", "command": "true"},
        )
        assert hook.status_code == 201, hook.text
        hooks[hook.json()["id"]] = target
        nt = await client.post(
            f"{base}/notifications",
            headers=admin,
            json={"name": f"n-{target}", "kind": "webhook", "url": "https://hooks.example/x"},
        )
        assert nt.status_code == 201, nt.text
        notifications[nt.json()["id"]] = target

    async def _remaining() -> tuple[set[str], set[str]]:
        async with SessionLocal() as session:
            hook_targets = (
                await session.execute(
                    select(Hook.target_id).where(Hook.id.in_([uuid.UUID(h) for h in hooks]))
                )
            ).scalars()
            nt_targets = (
                await session.execute(
                    select(NotificationTarget.target_id).where(
                        NotificationTarget.id.in_([uuid.UUID(n) for n in notifications])
                    )
                )
            ).scalars()
            return {str(t) for t in hook_targets}, {str(t) for t in nt_targets}

    assert (await client.delete(f"/api/v1/environments/{env_a}", headers=admin)).status_code == 204
    left = {stack, env_b, other_stack, other_env}
    assert await _remaining() == (left, left)

    assert (await client.delete(f"/api/v1/stacks/{stack}", headers=admin)).status_code == 204
    left = {other_stack, other_env}
    assert await _remaining() == (left, left)

    for action, rows in (("hook.deleted", hooks), ("notification_target.deleted", notifications)):
        audited = {}
        for row_id, target in rows.items():
            events = (
                await client.get(
                    "/api/v1/audit", headers=admin, params={"action": action, "target_id": row_id}
                )
            ).json()
            if events:
                [e] = events
                assert e["context"]["target_id"] == target
                audited[target] = e["context"]["reason"]
        assert audited == {
            env_a: "environment_deleted",
            env_b: "stack_deleted",
            stack: "stack_deleted",
        }, action
