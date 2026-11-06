from __future__ import annotations

import uuid

import httpx

from tests.conftest_phase2 import (
    event,
    login,
    make_env,
    make_proposed_run,
    make_stack,
    register_worker,
)

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _apply_commit(client, wh, admin, env_id, sha, **opts) -> None:
    """Drive a run on env_id to `finished` (apply) at commit `sha` (`opts`: plan options)."""
    run = (
        await client.post(
            f"/api/v1/environments/{env_id}/runs", headers=admin, json={"commit_sha": sha, **opts}
        )
    ).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    # confirm → apply → finished
    await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_finished", result={"outputs": {}})
    assert (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()[
        "state"
    ] == "finished"


async def test_promote_carries_the_applied_commit(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "promote-pool")
    stack = await make_stack(client, admin, "promote-stack")
    dev = await make_env(client, admin, stack, "dev", "dev")
    staging = await make_env(client, admin, stack, "staging", "staging")

    await _apply_commit(client, wh, admin, dev, "abc123")

    promoted = await client.post(
        f"/api/v1/environments/{staging}/promote", headers=admin, json={"from_environment_id": dev}
    )
    assert promoted.status_code == 201, promoted.text
    body = promoted.json()
    assert body["environment_id"] == staging
    assert body["commit_sha"] == "abc123"  # exact commit applied on dev
    assert body["type"] == "tracked"

    audit = await client.get("/api/v1/audit", headers=admin, params={"action": "run.promoted"})
    assert any(e["target_id"] == body["id"] for e in audit.json())


async def _plan_only(client, wh, run_id: str, sha: str, result: dict) -> None:
    """Drive a claimed-next run through its plan job at commit `sha`, to `finished` (plan-only)."""
    job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert job["job_id"] == run_id
    await event(client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": sha})
    await event(client, wh, run_id, "phase_finished", result=result)


async def test_promote_takes_the_last_apply_not_the_last_plan(client: httpx.AsyncClient) -> None:
    # A PR plan, a drift plan, an empty plan or a command run is no applied commit (#28): the
    # promotion pins the source's last apply, and refuses a source that has only those.
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "promote-last-pool")
    stack = await make_stack(client, admin, "promote-last")
    dev = await make_env(client, admin, stack, "dev", "dev")
    staging = await make_env(client, admin, stack, "staging", "staging")

    async def promote() -> httpx.Response:
        return await client.post(
            f"/api/v1/environments/{staging}/promote",
            headers=admin,
            json={"from_environment_id": dev},
        )

    pr_sha, empty_sha, applied_sha = "b" * 40, "c" * 40, "d" * 40
    await _plan_only(client, wh, await make_proposed_run(dev), pr_sha, CHANGES)
    empty = (await client.post(f"/api/v1/environments/{dev}/runs", headers=admin, json={})).json()
    await _plan_only(client, wh, empty["id"], empty_sha, {"has_changes": False})
    assert (await promote()).status_code == 409  # nothing applied yet

    await _apply_commit(client, wh, admin, dev, applied_sha)
    await _plan_only(client, wh, await make_proposed_run(dev), pr_sha, CHANGES)  # newer PR plan
    promoted = await promote()
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["commit_sha"] == applied_sha


async def test_promote_skips_a_targeted_apply(client: httpx.AsyncClient) -> None:
    # A `-target` apply changed only part of its commit (§9.7): the promotion source is the last
    # full apply. A replace-only apply applied its whole commit: it is a source.
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "promote-targeted-pool")
    stack = await make_stack(client, admin, "promote-targeted")
    dev = await make_env(client, admin, stack, "dev", "dev")
    staging = await make_env(client, admin, stack, "staging", "staging")

    async def promoted_sha() -> str | None:
        r = await client.post(
            f"/api/v1/environments/{staging}/promote",
            headers=admin,
            json={"from_environment_id": dev},
        )
        assert r.status_code in (201, 409), r.text
        if r.status_code == 409:
            return None
        # Free staging for the next promotion.
        await client.post(f"/api/v1/runs/{r.json()['id']}/cancel", headers=admin)
        return r.json()["commit_sha"]

    await _apply_commit(client, wh, admin, dev, "a" * 40, targets=["aws_s3_bucket.a"])
    assert await promoted_sha() is None  # only a targeted apply: nothing to promote

    await _apply_commit(client, wh, admin, dev, "b" * 40)
    await _apply_commit(client, wh, admin, dev, "c" * 40, targets=["aws_s3_bucket.a"])
    assert await promoted_sha() == "b" * 40

    await _apply_commit(client, wh, admin, dev, "d" * 40, replace=["aws_instance.web"])
    assert await promoted_sha() == "d" * 40


async def test_promote_requires_an_applied_source(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "promote-empty")
    dev = await make_env(client, admin, stack, "dev", "dev")
    staging = await make_env(client, admin, stack, "staging", "staging")
    r = await client.post(
        f"/api/v1/environments/{staging}/promote", headers=admin, json={"from_environment_id": dev}
    )
    assert r.status_code == 409  # dev has nothing applied yet


async def test_promote_rejects_cross_stack(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    s1 = await make_stack(client, admin, "promote-s1")
    s2 = await make_stack(client, admin, "promote-s2")
    dev = await make_env(client, admin, s1, "dev", "dev")
    other = await make_env(client, admin, s2, "dev", "dev")
    r = await client.post(
        f"/api/v1/environments/{other}/promote", headers=admin, json={"from_environment_id": dev}
    )
    assert r.status_code == 400


async def test_promote_missing_source_vs_target_404(client: httpx.AsyncClient) -> None:
    # The two ids get distinct 404 titles so a client can tell which one is wrong.
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "promote-404")
    staging = await make_env(client, admin, stack, "staging", "staging")
    bogus = str(uuid.uuid4())

    r = await client.post(
        f"/api/v1/environments/{staging}/promote",
        headers=admin,
        json={"from_environment_id": bogus},
    )
    assert r.status_code == 404
    assert r.json()["title"] == "Source environment not found"

    r = await client.post(
        f"/api/v1/environments/{bogus}/promote",
        headers=admin,
        json={"from_environment_id": staging},
    )
    assert r.status_code == 404
    assert r.json()["title"] == "Environment not found"


async def test_promote_source_still_space_gated(client: httpx.AsyncClient) -> None:
    # The custom 404 title must not weaken the access check on the source: a writer who can reach
    # the target but not the source's space is refused.
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer, member of the default space only
    space = await client.post("/api/v1/spaces", headers=admin, json={"name": "promote-other"})
    assert space.status_code == 201, space.text
    other_stack = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "space_id": space.json()["id"],
            "name": "promote-other-stack",
            "repo_url": "file:///repos/x",
            "tool_version": "1.12.0",
        },
    )
    source = await make_env(client, admin, other_stack.json()["id"], "dev", "dev")
    stack = await make_stack(client, admin, "promote-gated")
    target = await make_env(client, admin, stack, "staging", "staging")

    r = await client.post(
        f"/api/v1/environments/{target}/promote", headers=bob, json={"from_environment_id": source}
    )
    assert r.status_code == 403
