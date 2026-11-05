from __future__ import annotations

import uuid

import httpx
import pytest

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1}}


async def _dep(client, h, downstream_env, upstream_env, *, mock=None, policy="on_output_change"):
    refs = [{"output_name": "network_name", "input_name": "network_name", "mock_value": mock}]
    r = await client.post(
        f"/api/v1/environments/{downstream_env}/dependencies",
        headers=h,
        json={"upstream_env_id": upstream_env, "trigger_policy": policy, "references": refs},
    )
    return r


async def _set_output(env_id: str, name: str, value: str) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import SessionLocal
    from app.dependencies.service import canonical_hash
    from app.models.dependency import EnvOutput

    async with SessionLocal() as s:
        await s.execute(
            pg_insert(EnvOutput)
            .values(
                environment_id=uuid.UUID(env_id),
                name=name,
                value=value,
                value_hash=canonical_hash(value),
                sensitive=False,
            )
            .on_conflict_do_update(
                index_elements=["environment_id", "name"],
                set_={"value": value, "value_hash": canonical_hash(value)},
            )
        )
        await s.commit()


async def test_mock_used_and_blocks_apply(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-mock-dep")
    net = await make_stack(client, admin, "dep-network")
    net_dev = await make_env(client, admin, net, "dev", "dev")
    app = await make_stack(client, admin, "dep-app")
    app_dev = await make_env(client, admin, app, "dev", "dev")
    assert (await _dep(client, admin, app_dev, net_dev, mock="mock-net")).status_code == 201

    run = (await client.post(f"/api/v1/environments/{app_dev}/runs", headers=admin, json={})).json()
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["mock_inputs"] == {"TF_VAR_network_name": "mock-net"}
    assert payload["tfvars_json"]["network_name"] == "mock-net"

    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert detail["used_mocks"] is True
    assert detail["variable_provenance"]["TF_VAR_network_name"] == "mock"

    blocked = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert blocked.status_code == 409  # mock-apply disabled by default (§9.3)


async def test_real_value_beats_mock(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-real-dep")
    net = await make_stack(client, admin, "real-network")
    net_dev = await make_env(client, admin, net, "dev", "dev")
    app = await make_stack(client, admin, "real-app")
    app_dev = await make_env(client, admin, app, "dev", "dev")
    await _dep(client, admin, app_dev, net_dev, mock="mock-net")
    await _set_output(net_dev, "network_name", "real-net")

    await client.post(f"/api/v1/environments/{app_dev}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["tfvars_json"]["network_name"] == "real-net"
    assert payload["resolved_inputs"]["TF_VAR_network_name"] == "real-net"
    assert payload["mock_inputs"] == {}


async def test_missing_upstream_without_mock_fails_run(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-miss-dep")
    net = await make_stack(client, admin, "miss-network")
    net_dev = await make_env(client, admin, net, "dev", "dev")
    app = await make_stack(client, admin, "miss-app")
    app_dev = await make_env(client, admin, app, "dev", "dev")
    await _dep(client, admin, app_dev, net_dev, mock=None)

    run = (await client.post(f"/api/v1/environments/{app_dev}/runs", headers=admin, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 204  # nothing handed out
    detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert detail["state"] == "failed"
    assert "missing_upstream_output" in detail["error"]


async def test_anti_cycle(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    a = await make_stack(client, admin, "cycle-a")
    a_dev = await make_env(client, admin, a, "dev", "dev")
    b = await make_stack(client, admin, "cycle-b")
    b_dev = await make_env(client, admin, b, "dev", "dev")
    assert (await _dep(client, admin, a_dev, b_dev)).status_code == 201  # A depends on B
    cyclic = await _dep(client, admin, b_dev, a_dev)  # B depends on A → cycle
    assert cyclic.status_code == 422


async def test_cascade_triggers_downstream(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-cascade")
    net = await make_stack(client, admin, "casc-network")
    net_dev = await make_env(client, admin, net, "dev", "dev")
    app = await make_stack(client, admin, "casc-app")
    app_dev = await make_env(client, admin, app, "dev", "dev")
    await _dep(client, admin, app_dev, net_dev, mock="mock-net", policy="always")

    run = (await client.post(f"/api/v1/environments/{net_dev}/runs", headers=admin, json={})).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    await client.post("/worker/v1/jobs/claim", headers=wh)  # apply
    await event(
        client,
        wh,
        run["id"],
        "phase_finished",
        result={"outputs": {"network_name": {"value": "real-net", "sensitive": False}}},
    )

    downstream_runs = (
        await client.get(f"/api/v1/environments/{app_dev}/runs", headers=admin)
    ).json()
    assert len(downstream_runs) == 1
    assert downstream_runs[0]["triggered_by"] == "dependency"


async def _link_by_name(
    client: httpx.AsyncClient, h: dict[str, str], stack_id: str, upstream_stack_id: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/stacks/{stack_id}/dependencies/link-by-name",
        headers=h,
        json={"upstream_stack_id": upstream_stack_id},
    )


async def _upstreams(client: httpx.AsyncClient, h: dict[str, str], env_id: str) -> list[str]:
    deps = (await client.get(f"/api/v1/environments/{env_id}/dependencies", headers=h)).json()
    return [d["upstream_env_id"] for d in deps]


async def _created_audits(client: httpx.AsyncClient, admin: dict[str, str], env_id: str) -> int:
    rows = (
        await client.get(
            "/api/v1/audit",
            headers=admin,
            params={"action": "dependency.created", "target_id": env_id},
        )
    ).json()
    return len(rows)


async def _homonym_stacks(
    client: httpx.AsyncClient, admin: dict[str, str], prefix: str
) -> tuple[str, str, dict[str, str], dict[str, str]]:
    up = await make_stack(client, admin, f"{prefix}-up")
    down = await make_stack(client, admin, f"{prefix}-down")
    names = ("dev", "staging", "prod")
    up_envs = {n: await make_env(client, admin, up, n, "dev") for n in names}
    down_envs = {n: await make_env(client, admin, down, n, "dev") for n in names}
    return up, down, up_envs, down_envs


async def test_link_by_name_twice_is_idempotent(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    up, down, up_envs, down_envs = await _homonym_stacks(client, admin, "lbn-twice")

    first = await _link_by_name(client, admin, down, up)
    assert first.status_code == 200, first.text
    assert first.json() == {"created": 3}
    second = await _link_by_name(client, admin, down, up)
    assert second.status_code == 200, second.text  # was a 500 (PendingRollbackError)
    assert second.json() == {"created": 0}

    for name, env_id in down_envs.items():
        assert await _upstreams(client, admin, env_id) == [up_envs[name]]  # kept, no duplicate
        assert await _created_audits(client, admin, env_id) == 1


async def test_link_by_name_skips_pair_linked_by_hand(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    up, down, up_envs, down_envs = await _homonym_stacks(client, admin, "lbn-hand")
    assert (await _dep(client, admin, down_envs["staging"], up_envs["staging"])).status_code == 201

    r = await _link_by_name(client, admin, down, up)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 2}
    for name, env_id in down_envs.items():
        assert await _upstreams(client, admin, env_id) == [up_envs[name]]
        assert await _created_audits(client, admin, env_id) == 1
    # The hand-made edge is untouched (its reference survives).
    hand = (
        await client.get(f"/api/v1/environments/{down_envs['staging']}/dependencies", headers=admin)
    ).json()
    assert len(hand[0]["references"]) == 1


async def test_link_by_name_race_on_existing_edge_only_drops_that_edge(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.dependencies import router

    admin = await login(client, "admin")
    up, down, up_envs, down_envs = await _homonym_stacks(client, admin, "lbn-race")
    assert (await _dep(client, admin, down_envs["staging"], up_envs["staging"])).status_code == 201

    # Simulate a concurrent insert landing after the up-front load: the existing pair is unseen,
    # so its insert hits the unique constraint inside its SAVEPOINT.
    async def _stale(session: object) -> dict:
        return {}

    monkeypatch.setattr(router, "_load_adjacency", _stale)
    r = await _link_by_name(client, admin, down, up)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 2}
    for name, env_id in down_envs.items():
        assert await _upstreams(client, admin, env_id) == [up_envs[name]]
        assert await _created_audits(client, admin, env_id) == 1


async def test_link_by_name_skips_cycle_through_edge_of_same_request(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    up = await make_stack(client, admin, "lbn-cycle-up")
    down = await make_stack(client, admin, "lbn-cycle-down")
    u = {n: await make_env(client, admin, up, n, "dev") for n in ("a", "b")}
    d = {n: await make_env(client, admin, down, n, "dev") for n in ("a", "b")}
    # Existing edges D.b→U.a and D.a→U.b. Linking U.a→D.a and U.b→D.b in one request closes the
    # loop D.b→U.a→D.a→U.b→D.b: whichever homonymous edge comes second must be skipped.
    assert (await _dep(client, admin, u["a"], d["b"])).status_code == 201
    assert (await _dep(client, admin, u["b"], d["a"])).status_code == 201

    r = await _link_by_name(client, admin, down, up)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 1}
    linked = [n for n in ("a", "b") if await _upstreams(client, admin, d[n]) == [u[n]]]
    assert len(linked) == 1
    assert await _created_audits(client, admin, d[linked[0]]) == 1
    other = "b" if linked[0] == "a" else "a"
    assert await _upstreams(client, admin, d[other]) == []
    assert await _created_audits(client, admin, d[other]) == 0


async def test_graph_hides_edges_outside_caller_spaces(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # logs in first: not provisioned into the space below
    space = await client.post("/api/v1/spaces", headers=admin, json={"name": "graph-private"})
    assert space.status_code == 201, space.text
    private_stack = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "space_id": space.json()["id"],
            "name": "graph-private-stack",
            "repo_url": "file:///repos/x",
            "tool_version": "1.12.0",
        },
    )
    assert private_stack.status_code == 201, private_stack.text
    p_a = await make_env(client, admin, private_stack.json()["id"], "a", "dev")
    p_b = await make_env(client, admin, private_stack.json()["id"], "b", "dev")
    shared = await make_stack(client, admin, "graph-shared")  # default space: bob is a member
    s_a = await make_env(client, admin, shared, "a", "dev")
    s_b = await make_env(client, admin, shared, "b", "dev")
    s_c = await make_env(client, admin, shared, "c", "dev")

    visible = await client.post(
        f"/api/v1/environments/{s_b}/dependencies",
        headers=admin,
        json={
            "upstream_env_id": s_a,
            "references": [
                {"output_name": "o1", "input_name": "i1", "mock_value": "m"},
                {"output_name": "o2", "input_name": "i2"},
            ],
        },
    )
    assert visible.status_code == 201, visible.text
    bare = await client.post(
        f"/api/v1/environments/{s_c}/dependencies", headers=admin, json={"upstream_env_id": s_a}
    )
    assert bare.status_code == 201, bare.text
    private_edge = (await _dep(client, admin, p_b, p_a, mock="x")).json()["id"]
    cross_in = (await _dep(client, admin, s_c, p_a)).json()["id"]  # private → shared
    cross_out = (await _dep(client, admin, p_b, s_a)).json()["id"]  # shared → private

    bob_edges = {
        e["id"]: e for e in (await client.get("/api/v1/graph", headers=bob)).json()["edges"]
    }
    assert {private_edge, cross_in, cross_out}.isdisjoint(bob_edges)
    assert bob_edges[visible.json()["id"]]["references"] == 2
    assert bob_edges[visible.json()["id"]]["has_mock"] is True
    assert bob_edges[bare.json()["id"]]["references"] == 0
    assert bob_edges[bare.json()["id"]]["has_mock"] is False

    admin_edges = {
        e["id"]: e for e in (await client.get("/api/v1/graph", headers=admin)).json()["edges"]
    }
    assert {private_edge, cross_in, cross_out, visible.json()["id"]} <= admin_edges.keys()
    assert admin_edges[private_edge]["references"] == 1
    assert admin_edges[private_edge]["has_mock"] is True


# --- §9.2 steps 4-5: multi-parent gating and stop on failure within a run group ---


async def _diamond(client: httpx.AsyncClient, admin: dict, prefix: str) -> dict[str, str]:
    """root → a, root → b, a → c, b → c — `c` has two parents in the same group."""
    envs: dict[str, str] = {}
    for name in ("root", "a", "b", "c"):
        stack = await make_stack(client, admin, f"{prefix}-{name}")
        envs[name] = await make_env(client, admin, stack, "dev", "dev")
    for down, up in (("a", "root"), ("b", "root"), ("c", "a"), ("c", "b")):
        r = await _dep(client, admin, envs[down], envs[up], mock="m", policy="always")
        assert r.status_code == 201, r.text
    return envs


async def _claim(client: httpx.AsyncClient, wh: dict) -> dict:
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 200, r.text
    return r.json()


async def _plan(client, wh, admin, job: dict, *, changes: bool = True) -> None:
    await event(client, wh, job["job_id"], "phase_started", phase="planning")
    result = CHANGES if changes else {"has_changes": False, "summary": {}}
    await event(client, wh, job["job_id"], "phase_finished", result=result)


async def _apply(client, wh, admin, run_id: str) -> None:
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    job = await _claim(client, wh)
    assert job["job_id"] == run_id and job["phase"] == "apply"
    outputs = {"network_name": {"value": f"net-{run_id[:4]}", "sensitive": False}}
    await event(client, wh, run_id, "phase_finished", result={"outputs": outputs})


async def _env_runs(client, admin, env_id: str) -> list[dict]:
    return (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()


async def _start_diamond(client, admin, wh, envs) -> tuple[str, dict[str, dict]]:
    """Root planned + applied with downstream; returns the root run id and the two jobs a, b."""
    root = (
        await client.post(
            f"/api/v1/environments/{envs['root']}/runs",
            headers=admin,
            json={"with_downstream": True},
        )
    ).json()
    await _plan(client, wh, admin, await _claim(client, wh))
    await _apply(client, wh, admin, root["id"])
    jobs = {}
    for _ in range(2):
        job = await _claim(client, wh)
        name = "a" if job["environment"]["id"] == envs["a"] else "b"
        jobs[name] = job
    return root["id"], jobs


async def test_downstream_waits_for_all_its_parents(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-gating")
    envs = await _diamond(client, admin, "gate")
    root_id, jobs = await _start_diamond(client, admin, wh, envs)

    await _plan(client, wh, admin, jobs["a"])
    await _apply(client, wh, admin, jobs["a"]["job_id"])
    assert await _env_runs(client, admin, envs["c"]) == []  # b is still running: c waits

    await _plan(client, wh, admin, jobs["b"])
    await _apply(client, wh, admin, jobs["b"]["job_id"])
    c_runs = await _env_runs(client, admin, envs["c"])
    assert len(c_runs) == 1  # triggered once, by the last parent
    group = (await client.get(f"/api/v1/runs/{root_id}", headers=admin)).json()["run_group"]
    assert group == {"id": root_id, "status": "running"}  # c is queued
    child = (await client.get(f"/api/v1/runs/{c_runs[0]['id']}", headers=admin)).json()
    assert child["run_group_id"] == root_id and child["run_group"]["id"] == root_id


async def test_a_failed_parent_stops_its_branch(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-stop")
    envs = await _diamond(client, admin, "stop")
    root_id, jobs = await _start_diamond(client, admin, wh, envs)

    await _plan(client, wh, admin, jobs["a"])
    await _apply(client, wh, admin, jobs["a"]["job_id"])
    await event(client, wh, jobs["b"]["job_id"], "job_failed", phase="plan", result={"error": "x"})

    assert await _env_runs(client, admin, envs["c"]) == []  # never: a parent failed
    group = (await client.get(f"/api/v1/runs/{root_id}", headers=admin)).json()["run_group"]
    assert group["status"] == "partial_failure"


async def test_an_empty_plan_releases_a_waiting_downstream(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-empty")
    envs = await _diamond(client, admin, "empty")
    _, jobs = await _start_diamond(client, admin, wh, envs)

    await _plan(client, wh, admin, jobs["a"])
    await _apply(client, wh, admin, jobs["a"]["job_id"])
    await _plan(client, wh, admin, jobs["b"], changes=False)  # b: nothing to apply, finished

    assert len(await _env_runs(client, admin, envs["c"])) == 1  # a's apply triggers c


# --- review: decisions over the whole group, whatever the order; concurrent parents ---


async def _envs(client, admin, prefix: str, names: tuple[str, ...]) -> dict[str, str]:
    envs = {}
    for name in names:
        stack = await make_stack(client, admin, f"{prefix}-{name}")
        envs[name] = await make_env(client, admin, stack, "dev", "dev")
    return envs


async def _link(client, admin, envs, pairs) -> None:
    for down, up in pairs:
        r = await _dep(client, admin, envs[down], envs[up], mock="m", policy="always")
        assert r.status_code == 201, r.text


async def _drive_root(client, admin, wh, env_id: str) -> str:
    root = (
        await client.post(
            f"/api/v1/environments/{env_id}/runs", headers=admin, json={"with_downstream": True}
        )
    ).json()
    await _plan(client, wh, admin, await _claim(client, wh))
    await _apply(client, wh, admin, root["id"])
    return root["id"]


async def test_a_downstream_waits_for_an_indirect_parent(client: httpx.AsyncClient) -> None:
    """r → a → d and r → d: d waits for a, whatever order r's edges are read in."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-indirect")
    envs = await _envs(client, admin, "indir", ("r", "a", "d"))
    # r → d is created first: read first, it must still not trigger d before a has run.
    await _link(client, admin, envs, (("d", "r"), ("a", "r"), ("d", "a")))
    await _drive_root(client, admin, wh, envs["r"])

    assert await _env_runs(client, admin, envs["d"]) == []  # a is queued: d waits
    job = await _claim(client, wh)
    assert job["environment"]["id"] == envs["a"]
    await _plan(client, wh, admin, job)
    await _apply(client, wh, admin, job["job_id"])
    assert len(await _env_runs(client, admin, envs["d"])) == 1


async def test_a_failure_stops_everything_reachable(client: httpx.AsyncClient) -> None:
    """r → y → x → d and r → d: y fails, x never runs, and d — reachable from y — never runs."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-reach")
    envs = await _envs(client, admin, "reach", ("r", "y", "x", "d"))
    await _link(client, admin, envs, (("y", "r"), ("x", "y"), ("d", "x"), ("d", "r")))
    root_id = await _drive_root(client, admin, wh, envs["r"])

    job = await _claim(client, wh)
    assert job["environment"]["id"] == envs["y"]
    await event(client, wh, job["job_id"], "job_failed", phase="plan", result={"error": "x"})
    assert await _env_runs(client, admin, envs["x"]) == []
    assert await _env_runs(client, admin, envs["d"]) == []
    group = (await client.get(f"/api/v1/runs/{root_id}", headers=admin)).json()["run_group"]
    assert group["status"] == "partial_failure"


async def test_parents_finishing_together_trigger_once(client: httpx.AsyncClient) -> None:
    """Both parents of c finish in two transactions that overlap in the DB: each one's transition
    is made (uncommitted) before either cascades. The lock on the group's root serializes the two
    cascades, so the second sees the first's outcome: c is triggered exactly once (without the
    lock, each sees the other parent still applying and c never runs)."""
    import asyncio

    from sqlalchemy import text

    from app.db import SessionLocal
    from app.dependencies.service import cascade
    from app.enums import RunEventActor, RunState
    from app.models.run import Run
    from app.runs.transition import transition

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-conc")
    envs = await _diamond(client, admin, "conc")
    _, jobs = await _start_diamond(client, admin, wh, envs)
    for job in jobs.values():
        await _plan(client, wh, admin, job)
        run_id = job["job_id"]
        assert (
            await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
        ).status_code == 200
        assert (await _claim(client, wh))["job_id"] == run_id  # applying

    both_finished = asyncio.Barrier(2)
    cascaded: list[str] = []

    async def hold_commit() -> None:
        """Keep this transaction open until the other cascade has read the group too (it would,
        without the lock) or waits on the lock — so the two cascades really overlap."""
        while len(cascaded) < 2:
            async with SessionLocal() as probe:  # a fresh pg_stat_activity snapshot each time
                blocked = await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    )
                )
            if blocked:
                return
            await asyncio.sleep(0.02)

    async def finish(run_id: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(Run, uuid.UUID(run_id))
            assert run is not None
            await transition(
                session,
                run,
                RunState.finished,
                actor=RunEventActor.worker,
                audit_action="run.applied",
            )
            await both_finished.wait()  # both transitions made, neither committed
            await cascade(session, run)
            cascaded.append(run_id)
            await hold_commit()
            await session.commit()

    await asyncio.wait_for(asyncio.gather(*(finish(j["job_id"]) for j in jobs.values())), 30)
    assert len(await _env_runs(client, admin, envs["c"])) == 1


# --- review round 2: stop conditions, policies, parent, decision stability, protections ---


async def _dep_io(client, h, down: str, up: str, policy: str, output: str, input_: str) -> None:
    refs = [{"output_name": output, "input_name": input_, "mock_value": "m"}]
    r = await client.post(
        f"/api/v1/environments/{down}/dependencies",
        headers=h,
        json={"upstream_env_id": up, "trigger_policy": policy, "references": refs},
    )
    assert r.status_code == 201, r.text


async def _apply_out(client, wh, admin, run_id: str, outputs: dict[str, str]) -> None:
    assert (await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)).status_code == 200
    job = await _claim(client, wh)
    assert job["job_id"] == run_id and job["phase"] == "apply"
    result = {"outputs": {k: {"value": v, "sensitive": False} for k, v in outputs.items()}}
    await event(client, wh, run_id, "phase_finished", result=result)


async def _root_applied(client, admin, wh, root_env: str) -> tuple[str, dict[str, dict]]:
    """A group rooted at `root_env`, planned and applied; returns its id and the jobs it queued
    (claimed), by environment id."""
    root_id = await _drive_root(client, admin, wh, root_env)
    jobs: dict[str, dict] = {}
    while (r := await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200:
        jobs[r.json()["environment"]["id"]] = r.json()
    return root_id, jobs


async def _group(client, admin, run_id: str) -> dict:
    return (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()["run_group"]


async def test_a_discarded_parent_stops_its_branch(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-discard")
    envs = await _diamond(client, admin, "discard")
    root_id, jobs = await _start_diamond(client, admin, wh, envs)

    await _plan(client, wh, admin, jobs["b"])  # unconfirmed
    discard = await client.post(f"/api/v1/runs/{jobs['b']['job_id']}/discard", headers=admin)
    assert discard.status_code == 200, discard.text
    await _plan(client, wh, admin, jobs["a"])
    await _apply(client, wh, admin, jobs["a"]["job_id"])  # its cascade sees b discarded

    assert await _env_runs(client, admin, envs["c"]) == []
    assert (await _group(client, admin, root_id))["status"] == "partial_failure"


async def test_a_canceled_parent_stops_its_branch(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-cancel")
    envs = await _diamond(client, admin, "cancel")
    root_id = await _drive_root(client, admin, wh, envs["root"])

    b_run = (await _env_runs(client, admin, envs["b"]))[0]
    assert b_run["state"] == "queued"
    cancel = await client.post(f"/api/v1/runs/{b_run['id']}/cancel", headers=admin)
    assert cancel.status_code == 200, cancel.text
    job = await _claim(client, wh)
    assert job["environment"]["id"] == envs["a"]
    await _plan(client, wh, admin, job)
    await _apply(client, wh, admin, job["job_id"])  # its cascade sees b canceled

    assert await _env_runs(client, admin, envs["c"]) == []
    assert (await _group(client, admin, root_id))["status"] == "partial_failure"


async def test_never_edges_in_a_multi_parent_graph(client: httpx.AsyncClient) -> None:
    """a → c `never`, b → c `always`: c runs once, parented by b; a → d and b → d `never`: d is
    skipped. The last parent to finish (a) is not c's parent: its edge did not pass."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-never")
    envs = await _envs(client, admin, "never", ("root", "a", "b", "c", "d"))
    await _link(client, admin, envs, (("a", "root"), ("b", "root")))
    for down, up, policy in (
        ("c", "a", "never"),
        ("c", "b", "always"),
        ("d", "a", "never"),
        ("d", "b", "never"),
    ):
        await _dep_io(client, admin, envs[down], envs[up], policy, "out", f"in_{up}")
    root_id, jobs = await _root_applied(client, admin, wh, envs["root"])

    for name in ("b", "a"):
        await _plan(client, wh, admin, jobs[envs[name]])
        await _apply_out(client, wh, admin, jobs[envs[name]]["job_id"], {"out": name})

    c_runs = await _env_runs(client, admin, envs["c"])
    assert len(c_runs) == 1
    assert c_runs[0]["parent_run_id"] == jobs[envs["b"]]["job_id"]
    assert await _env_runs(client, admin, envs["d"]) == []
    assert (await _group(client, admin, root_id))["status"] == "running"  # c is queued


async def test_on_output_change_in_a_multi_parent_graph(client: httpx.AsyncClient) -> None:
    """a → c and b → c `on_output_change`: c runs when one parent's output differs from what it
    last consumed, once, parented by that parent — even when the other one finishes last."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ooc")
    envs = await _envs(client, admin, "ooc", ("root", "a", "b", "c"))
    await _link(client, admin, envs, (("a", "root"), ("b", "root")))
    for up in ("a", "b"):
        await _dep_io(client, admin, envs["c"], envs[up], "on_output_change", "out", f"in_{up}")

    async def round_(outputs: dict[str, str]) -> tuple[str, dict[str, str]]:
        root_id, jobs = await _root_applied(client, admin, wh, envs["root"])
        for name in ("b", "a"):  # a finishes last
            await _plan(client, wh, admin, jobs[envs[name]])
            await _apply_out(client, wh, admin, jobs[envs[name]]["job_id"], {"out": outputs[name]})
        return root_id, {n: jobs[envs[n]]["job_id"] for n in ("a", "b")}

    await round_({"a": "A1", "b": "B1"})  # c never ran: triggered
    assert len(await _env_runs(client, admin, envs["c"])) == 1
    c_job = await _claim(client, wh)
    assert c_job["resolved_inputs"] == {"TF_VAR_in_a": "A1", "TF_VAR_in_b": "B1"}
    await _plan(client, wh, admin, c_job)
    await _apply_out(client, wh, admin, c_job["job_id"], {})

    root_id, _ = await round_({"a": "A1", "b": "B1"})  # nothing changed: skipped
    assert len(await _env_runs(client, admin, envs["c"])) == 1
    assert (await _group(client, admin, root_id))["status"] == "succeeded"

    _, runs = await round_({"a": "A1", "b": "B2"})  # b changed
    c_runs = await _env_runs(client, admin, envs["c"])
    assert len(c_runs) == 2
    assert c_runs[0]["parent_run_id"] == runs["b"]  # newest first; not a, which finished last


async def test_an_empty_plan_is_never_the_parent(client: httpx.AsyncClient) -> None:
    """a applies, b's plan is empty and releases c: c's parent is a, the run that applied."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-empty-parent")
    envs = await _diamond(client, admin, "emptyp")
    _, jobs = await _start_diamond(client, admin, wh, envs)

    await _plan(client, wh, admin, jobs["a"])
    await _apply(client, wh, admin, jobs["a"]["job_id"])
    await _plan(client, wh, admin, jobs["b"], changes=False)

    c_runs = await _env_runs(client, admin, envs["c"])
    assert len(c_runs) == 1
    assert c_runs[0]["parent_run_id"] == jobs["a"]["job_id"]


async def test_a_skipped_downstream_stays_skipped(client: httpx.AsyncClient) -> None:
    """a → c `on_output_change` is decided (skipped) when a finishes. The upstream's outputs then
    change outside the group: b finishing later in the group does not revisit c."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-stable")
    envs = await _envs(client, admin, "stable", ("root", "a", "b", "c"))
    await _link(client, admin, envs, (("a", "root"), ("b", "root")))
    await _dep_io(client, admin, envs["c"], envs["a"], "on_output_change", "out", "in_a")

    _, jobs = await _root_applied(client, admin, wh, envs["root"])  # c first runs on A1
    for name in ("a", "b"):
        await _plan(client, wh, admin, jobs[envs[name]])
        await _apply_out(client, wh, admin, jobs[envs[name]]["job_id"], {"out": "A1"})
    c_job = await _claim(client, wh)
    await _plan(client, wh, admin, c_job)
    await _apply_out(client, wh, admin, c_job["job_id"], {})

    root_id, jobs = await _root_applied(client, admin, wh, envs["root"])
    await _plan(client, wh, admin, jobs[envs["a"]])
    await _apply_out(client, wh, admin, jobs[envs["a"]]["job_id"], {"out": "A1"})  # c: skipped
    await _set_output(envs["a"], "out", "A2")  # e.g. a manual apply of a, outside the group
    await _plan(client, wh, admin, jobs[envs["b"]])
    await _apply_out(client, wh, admin, jobs[envs["b"]]["job_id"], {"out": "x"})

    assert len(await _env_runs(client, admin, envs["c"])) == 1  # no second run in the group
    assert (await _group(client, admin, root_id))["status"] == "succeeded"


async def test_a_protected_downstream_stops_at_unconfirmed(client: httpx.AsyncClient) -> None:
    """c is `autodeploy` but `protected`: its cascade run stops at unconfirmed for a human."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-protected")
    envs = await _envs(client, admin, "prot", ("root", "a", "b"))
    c_stack = await make_stack(client, admin, "prot-c")
    envs["c"] = await make_env(
        client, admin, c_stack, "dev", "dev", autodeploy=True, protected=True
    )
    await _link(client, admin, envs, (("a", "root"), ("b", "root"), ("c", "a"), ("c", "b")))
    root_id, jobs = await _start_diamond(client, admin, wh, envs)
    for name in ("a", "b"):
        await _plan(client, wh, admin, jobs[name])
        await _apply(client, wh, admin, jobs[name]["job_id"])

    job = await _claim(client, wh)
    assert job["environment"]["id"] == envs["c"] and job["phase"] == "plan"
    await _plan(client, wh, admin, job)  # a non-empty plan, no mock, no warn

    c_run = (await client.get(f"/api/v1/runs/{job['job_id']}", headers=admin)).json()
    assert c_run["triggered_by"] == "dependency"
    assert c_run["state"] == "unconfirmed"  # never auto-confirmed on a protected env
    assert c_run["confirmed_by_user_id"] is None
    assert (await _group(client, admin, root_id))["status"] == "running"


async def test_group_status_counts_the_runs_the_caller_can_read(client: httpx.AsyncClient) -> None:
    """A group crossing into a space bob is not a member of: bob's status ignores its runs there."""
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # logs in first: not provisioned into the space below
    wh = await register_worker(client, admin, "pool-group-space")
    space = await client.post("/api/v1/spaces", headers=admin, json={"name": "group-private"})
    assert space.status_code == 201, space.text
    private_stack = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "space_id": space.json()["id"],
            "name": "group-private-stack",
            "repo_url": "file:///repos/x",
            "tool_version": "1.12.0",
        },
    )
    assert private_stack.status_code == 201, private_stack.text
    hidden = await make_env(client, admin, private_stack.json()["id"], "dev", "dev")
    shared = await make_env(
        client, admin, await make_stack(client, admin, "group-shared"), "dev", "dev"
    )
    await _link(client, admin, {"h": hidden, "s": shared}, (("h", "s"),))

    root_id = await _drive_root(client, admin, wh, shared)  # hidden's run is queued
    assert (await _group(client, admin, root_id))["status"] == "running"
    assert (await _group(client, bob, root_id)) == {"id": root_id, "status": "succeeded"}


async def test_on_output_change_compares_with_the_last_apply(client: httpx.AsyncClient) -> None:
    """The `on_output_change` baseline is the downstream's last *applied* run (§9.2): a proposed
    plan that read the new upstream value in between deployed nothing, so it must not suppress
    the cascade that brings that value to the downstream."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-ooc-baseline")
    envs = await _envs(client, admin, "oocb", ("up", "down"))
    await _dep_io(client, admin, envs["down"], envs["up"], "on_output_change", "out", "in_up")

    async def apply_up(value: str) -> None:
        run = await client.post(
            f"/api/v1/environments/{envs['up']}/runs", headers=admin, json={"with_downstream": True}
        )
        await _plan(client, wh, admin, await _claim(client, wh))
        await _apply_out(client, wh, admin, run.json()["id"], {"out": value})

    # down applies V1.
    await apply_up("V1")
    down_job = await _claim(client, wh)
    assert down_job["resolved_inputs"] == {"TF_VAR_in_up": "V1"}
    await _plan(client, wh, admin, down_job)
    await _apply_out(client, wh, admin, down_job["job_id"], {})

    # The upstream's output moves to V2, and a proposed plan of down reads it — and finishes.
    await _set_output(envs["up"], "out", "V2")
    proposed = await client.post(
        f"/api/v1/environments/{envs['down']}/runs", headers=admin, json={"type": "proposed"}
    )
    job = await _claim(client, wh)
    assert job["job_id"] == proposed.json()["id"]
    assert job["resolved_inputs"] == {"TF_VAR_in_up": "V2"}
    await _plan(client, wh, admin, job)
    assert (await _env_runs(client, admin, envs["down"]))[0]["state"] == "finished"

    # The upstream applies V2: down still runs V1, so the cascade must trigger it.
    await apply_up("V2")
    latest = (await _env_runs(client, admin, envs["down"]))[0]
    assert latest["triggered_by"] == "dependency" and latest["state"] == "queued"
