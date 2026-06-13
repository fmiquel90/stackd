from __future__ import annotations

import uuid

import httpx

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
