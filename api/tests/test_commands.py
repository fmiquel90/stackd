from __future__ import annotations

import httpx

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


async def _command(client, h, env_id, command, args=None):
    return await client.post(
        f"/api/v1/environments/{env_id}/commands",
        headers=h,
        json={"command": command, "args": args or []},
    )


async def test_command_allowlist_rejected(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "cmd-allowlist")
    env = await make_env(client, admin, stack, "dev", "dev")
    # `apply` is not a runnable ad-hoc command.
    r = await _command(client, admin, env, "apply")
    assert r.status_code == 400, r.text


async def test_readonly_command_allowed_for_writer(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer
    stack = await make_stack(client, admin, "cmd-readonly")
    env = await make_env(client, admin, stack, "dev", "dev")
    r = await _command(client, bob, env, "output")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "command"
    assert body["command"] == {"name": "output", "args": []}
    assert body["state"] == "queued"


async def test_mutating_command_requires_can_apply(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # writer → cannot apply
    stack = await make_stack(client, admin, "cmd-mutating")
    env = await make_env(client, admin, stack, "dev", "dev")

    denied = await _command(client, bob, env, "state rm", ["aws_s3_bucket.old"])
    assert denied.status_code == 403, denied.text

    ok = await _command(client, admin, env, "state rm", ["aws_s3_bucket.old"])
    assert ok.status_code == 201, ok.text


async def test_command_run_lifecycle(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "cmd-pool")
    stack = await make_stack(client, admin, "cmd-lifecycle")
    env = await make_env(client, admin, stack, "dev", "dev")

    run = (await _command(client, admin, env, "state list")).json()

    # The worker claims it as a `command` job carrying the subcommand.
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    assert payload["phase"] == "command"
    assert payload["command"] == {"name": "state list", "args": []}

    await event(client, wh, run["id"], "phase_started", phase="running")
    state = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()["state"]
    assert state == "running"

    await event(client, wh, run["id"], "phase_finished", result={"command": "state list"})
    final = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert final["state"] == "finished"

    audit = await client.get(
        "/api/v1/audit", headers=admin, params={"target_kind": "run", "target_id": run["id"]}
    )
    actions = {e["action"] for e in audit.json()}
    assert {"run.command_triggered", "run.command_executed"} <= actions


def _state_token_ttl(payload: dict) -> int:
    from app.statebackend.tokens import decode_state_token

    claims = decode_state_token(payload["backend"]["password"])
    return claims["exp"] - claims["iat"]


async def test_mutating_command_gets_apply_window_state_token(client: httpx.AsyncClient) -> None:
    """A mutating command holds apply credentials from its claim (§4.3), so its read-write state
    token must expire with the apply window — not the 6 h plan window — or a reclaimed command's
    worker could keep writing state over the next run's (split-brain)."""
    from app.config import get_settings

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "cmd-ttl-pool")
    stack = await make_stack(client, admin, "cmd-ttl-mutating")
    env = await make_env(client, admin, stack, "dev", "dev")
    run = (await _command(client, admin, env, "state rm", ["aws_s3_bucket.old"])).json()

    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    assert payload["job_id"] == run["id"]
    assert payload["phase"] == "command"
    assert _state_token_ttl(payload) == get_settings().apply_reclaim_window_seconds


async def test_readonly_command_keeps_long_state_token(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "cmd-ttl-ro-pool")
    stack = await make_stack(client, admin, "cmd-ttl-readonly")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _command(client, admin, env, "state list")

    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    assert _state_token_ttl(claim.json()) == 6 * 3600
