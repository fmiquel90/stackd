from __future__ import annotations

import httpx
import pytest

from app.secret_sources import providers
from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _space_id(client: httpx.AsyncClient, admin: dict, stack_id: str) -> str:
    return (await client.get(f"/api/v1/stacks/{stack_id}", headers=admin)).json()["space_id"]


async def _make_source(client, admin, space_id, name="proton") -> str:
    r = await client.post(
        f"/api/v1/spaces/{space_id}/secret-sources",
        headers=admin,
        json={"name": name, "provider": "proton_pass", "bootstrap_secret": "pat_xyz"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_ref_var(client, admin, env_id, source_id, *, name, fallback_mode, fallback=None):
    body = {
        "kind": "terraform",
        "name": name,
        "secret_source_id": source_id,
        "secret_ref": f"pass://vault/{name}/value",
        "secret_fallback_mode": fallback_mode,
    }
    if fallback is not None:
        body["secret_fallback"] = fallback
    r = await client.post(f"/api/v1/environments/{env_id}/variables", headers=admin, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_reference_resolved_live_is_masked(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-sref")
    stack = await make_stack(client, admin, "sref-stack")
    space = await _space_id(client, admin, stack)
    env_id = await make_env(client, admin, stack, "dev", "dev")
    source = await _make_source(client, admin, space, "proton-live")
    await _make_ref_var(client, admin, env_id, source, name="dbpass", fallback_mode="error")

    async def fake(_src, ref):  # provider up → live value
        assert ref == "pass://vault/dbpass/value"
        return "s3cr3t-live"

    monkeypatch.setattr(providers, "fetch_secret", fake)

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()

    assert payload["tfvars_json"]["dbpass"] == "s3cr3t-live"
    assert "s3cr3t-live" in payload["mask_values"]  # the agent redacts it in logs
    got = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert got["used_secret_fallback"] is False
    assert got["variable_provenance"]["TF_VAR_dbpass"] == "secret:proton-live"


async def test_static_fallback_used_and_blocks_apply(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-sfb")
    stack = await make_stack(client, admin, "sfb-stack")
    space = await _space_id(client, admin, stack)
    env_id = await make_env(client, admin, stack, "dev", "dev")
    source = await _make_source(client, admin, space, "proton-sfb")
    await _make_ref_var(
        client, admin, env_id, source, name="dbpass", fallback_mode="static", fallback="fallbackval"
    )

    async def down(_src, _ref):
        raise providers.SecretUnavailable("provider unreachable")

    monkeypatch.setattr(providers, "fetch_secret", down)

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["tfvars_json"]["dbpass"] == "fallbackval"

    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    got = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert got["state"] == "unconfirmed"
    assert got["used_secret_fallback"] is True
    assert got["variable_provenance"]["TF_VAR_dbpass"] == "secret_fallback:proton-sfb"

    # Apply blocked while the env hasn't opted into fallback applies.
    blocked = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert blocked.status_code == 409

    # Opt in → apply allowed.
    await client.patch(
        f"/api/v1/environments/{env_id}", headers=admin, json={"allow_fallback_apply": True}
    )
    ok = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert ok.status_code == 200


async def test_error_mode_fails_the_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-serr")
    stack = await make_stack(client, admin, "serr-stack")
    space = await _space_id(client, admin, stack)
    env_id = await make_env(client, admin, stack, "dev", "dev")
    source = await _make_source(client, admin, space, "proton-serr")
    await _make_ref_var(client, admin, env_id, source, name="dbpass", fallback_mode="error")

    async def down(_src, _ref):
        raise providers.SecretUnavailable("provider unreachable")

    monkeypatch.setattr(providers, "fetch_secret", down)

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 204  # fail-closed: nothing handed to the worker
    got = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert got["state"] == "failed"


async def test_break_glass_override(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-sbg")
    stack = await make_stack(client, admin, "sbg-stack")
    space = await _space_id(client, admin, stack)
    env_id = await make_env(client, admin, stack, "dev", "dev")
    source = await _make_source(client, admin, space, "proton-sbg")
    await _make_ref_var(client, admin, env_id, source, name="dbpass", fallback_mode="break_glass")

    async def down(_src, _ref):
        raise providers.SecretUnavailable("provider unreachable")

    monkeypatch.setattr(providers, "fetch_secret", down)

    # Operator supplies the value inline at trigger time (requires apply permission).
    run = (
        await client.post(
            f"/api/v1/environments/{env_id}/runs",
            headers=admin,
            json={"secret_overrides": {"dbpass": "manual-val"}},
        )
    ).json()
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["tfvars_json"]["dbpass"] == "manual-val"
    assert "manual-val" in payload["mask_values"]
    got = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert got["used_secret_fallback"] is True
    assert got["variable_provenance"]["TF_VAR_dbpass"] == "secret_override:proton-sbg"


async def test_override_requires_apply_permission(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer, cannot confirm applies
    stack = await make_stack(client, admin, "sperm-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    refused = await client.post(
        f"/api/v1/environments/{env_id}/runs",
        headers=bob,
        json={"secret_overrides": {"dbpass": "x"}},
    )
    assert refused.status_code == 403


async def test_source_crud_hides_bootstrap_and_gates_writes(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer
    stack = await make_stack(client, admin, "scrud-stack")
    space = await _space_id(client, admin, stack)

    # A writer cannot create a source (admin only).
    forbidden = await client.post(
        f"/api/v1/spaces/{space}/secret-sources",
        headers=bob,
        json={"name": "p", "provider": "proton_pass", "bootstrap_secret": "tok"},
    )
    assert forbidden.status_code == 403

    source = await _make_source(client, admin, space, "proton-scrud")
    listing = (await client.get(f"/api/v1/spaces/{space}/secret-sources", headers=admin)).json()
    created = next(s for s in listing if s["id"] == source)
    assert "bootstrap_secret" not in created  # write-only, never serialized
    assert "bootstrap_secret_encrypted" not in created


async def test_source_in_use_cannot_be_deleted(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "suse-stack")
    space = await _space_id(client, admin, stack)
    env_id = await make_env(client, admin, stack, "dev", "dev")
    source = await _make_source(client, admin, space, "proton-suse")
    await _make_ref_var(client, admin, env_id, source, name="dbpass", fallback_mode="error")

    blocked = await client.delete(f"/api/v1/spaces/{space}/secret-sources/{source}", headers=admin)
    assert blocked.status_code == 409


async def test_fallback_appearing_at_apply_is_gated(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider up at plan (live value, confirm passes), down at apply → the apply re-resolves via
    fallback; the §15.5 gate must re-fire and fail the run rather than apply a non-real secret."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-c1")
    stack = await make_stack(client, admin, "c1-stack")
    space = await _space_id(client, admin, stack)
    env_id = await make_env(client, admin, stack, "dev", "dev")
    source = await _make_source(client, admin, space, "proton-c1")
    await _make_ref_var(
        client, admin, env_id, source, name="dbpass", fallback_mode="static", fallback="fb"
    )

    async def up(_src, _ref):
        return "live-val"

    monkeypatch.setattr(providers, "fetch_secret", up)
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)  # plan, provider up
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    got = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert got["used_secret_fallback"] is False
    assert (
        await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    ).status_code == 200

    async def down(_src, _ref):
        raise providers.SecretUnavailable("provider unreachable")

    monkeypatch.setattr(providers, "fetch_secret", down)
    apply_claim = await client.post("/worker/v1/jobs/claim", headers=wh)  # apply, provider down
    assert apply_claim.status_code == 204
    failed = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert failed["state"] == "failed"
