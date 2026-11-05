from __future__ import annotations

import httpx


async def _admin(client: httpx.AsyncClient) -> dict[str, str]:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_register_reuses_row_for_same_name(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    token = (
        await client.post("/api/v1/worker-pools", headers=h, json={"name": "reuse-pool"})
    ).json()["token"]
    th = {"Authorization": f"Bearer {token}"}

    r1 = await client.post(
        "/worker/v1/register", headers=th, json={"name": "agent-a", "version": "1"}
    )
    r2 = await client.post(
        "/worker/v1/register", headers=th, json={"name": "agent-a", "version": "2"}
    )
    assert r1.status_code == 200 and r2.status_code == 200, r2.text

    # Re-registering the same name reuses the row (same id), not a fresh duplicate.
    assert r1.json()["worker_id"] == r2.json()["worker_id"]

    mine = [
        w for w in (await client.get("/api/v1/workers", headers=h)).json() if w["name"] == "agent-a"
    ]
    assert len(mine) == 1
    assert mine[0]["version"] == "2"  # row updated in place


async def _register(
    client: httpx.AsyncClient,
    h: dict[str, str],
    pool: str,
    *,
    pool_labels: dict | None = None,
    labels: dict | None = None,
    tools: dict | None = None,
) -> tuple[str, dict[str, str]]:
    token = (
        await client.post(
            "/api/v1/worker-pools", headers=h, json={"name": pool, "labels": pool_labels}
        )
    ).json()["token"]
    reg = await client.post(
        "/worker/v1/register",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "w", "runner": "local", "labels": labels, "tools": tools},
    )
    assert reg.status_code == 200, reg.text
    return reg.json()["worker_id"], {"Authorization": f"Bearer {reg.json()['worker_token']}"}


async def test_effective_labels_merge_pool_and_worker(client: httpx.AsyncClient) -> None:
    # SPECS §3.7/§7.2: the worker's own labels, the pool's on top (the pool wins per key: admin-set,
    # while the worker's are self-declared).
    h = await _admin(client)
    wid, _ = await _register(
        client,
        h,
        "labels-merge-pool",
        pool_labels={"tier": "prod", "region": "eu-west-1"},
        labels={"region": "eu-west-3", "gpu": "yes"},
    )
    bare_id, _ = await _register(client, h, "labels-none-pool")
    workers = {w["id"]: w for w in (await client.get("/api/v1/workers", headers=h)).json()}
    assert workers[wid]["labels"] == {"tier": "prod", "region": "eu-west-1", "gpu": "yes"}
    assert workers[bare_id]["labels"] is None


async def test_labelled_env_claimed_only_by_a_labelled_worker(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    # A tool version no other test's stack uses: these workers can only ever claim this test's run.
    tools = {"opentofu": ["7.7.7"]}
    _, bare = await _register(client, h, "labels-bare-pool", tools=tools)
    _, other = await _register(client, h, "labels-other-pool", labels={"tier": "x"}, tools=tools)
    _, own = await _register(client, h, "labels-own-pool", labels={"tier": "prod"}, tools=tools)
    _, via_pool = await _register(
        client, h, "labels-prod-pool", pool_labels={"tier": "prod"}, tools=tools
    )
    stack = await client.post(
        "/api/v1/stacks",
        headers=h,
        json={"name": "labels-claim", "repo_url": "file:///repos/x", "tool_version": "7.7.7"},
    )
    assert stack.status_code == 201, stack.text
    runs = []
    for name in ("a", "b"):
        env = await client.post(
            f"/api/v1/stacks/{stack.json()['id']}/environments",
            headers=h,
            json={"name": name, "tier": "dev", "branch": "main", "labels": {"tier": "prod"}},
        )
        assert env.status_code == 201, env.text
        run = await client.post(f"/api/v1/environments/{env.json()['id']}/runs", headers=h, json={})
        runs.append(run.json()["id"])

    # No labels, or the wrong ones: nothing to claim.
    assert (await client.post("/worker/v1/jobs/claim", headers=bare)).status_code == 204
    assert (await client.post("/worker/v1/jobs/claim", headers=other)).status_code == 204
    # A worker carrying the env's labels — its own, or inherited from its pool — takes a run.
    claimed = []
    for wh in (own, via_pool):
        job = await client.post("/worker/v1/jobs/claim", headers=wh)
        assert job.status_code == 200, job.text
        claimed.append(job.json()["job_id"])
    assert sorted(claimed) == sorted(runs)
