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
