from __future__ import annotations

import base64
import json

import httpx
from moto import mock_aws

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


def _basic(token: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"env:{token}".encode()).decode()}


def _state_doc(serial: int) -> bytes:
    return json.dumps({"version": 4, "serial": serial, "lineage": "cmd", "outputs": {}}).encode()


async def _claim_running_command(client, admin, wh, env, command, args=None) -> dict:
    """Trigger a command run, claim it and report it running; returns the claim payload."""
    run = (await _command(client, admin, env, command, args)).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    assert payload["job_id"] == run["id"]
    await event(client, wh, run["id"], "phase_started", phase="running")
    return payload


async def test_readonly_command_state_token_is_read_only(client: httpx.AsyncClient) -> None:
    """A read-only command (output, show, state list, …) only reads state: a `ro` token and no lock
    address — it can neither write state nor lock it (§11.2)."""
    from app.statebackend.tokens import decode_state_token

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "cmd-scope-ro-pool")
    stack = await make_stack(client, admin, "cmd-scope-readonly")
    env = await make_env(client, admin, stack, "dev", "dev")
    backend = (await _claim_running_command(client, admin, wh, env, "state list"))["backend"]
    assert not {"lock_address", "unlock_address"} & set(backend)
    token = backend["password"]
    assert decode_state_token(token)["scope"] == "ro"

    with mock_aws():
        assert (await client.get(f"/state/v1/{env}", headers=_basic(token))).status_code == 404
        post = await client.post(f"/state/v1/{env}", headers=_basic(token), content=_state_doc(1))
        assert post.status_code == 403
        lock = await client.request(
            "LOCK", f"/state/v1/{env}/lock", headers=_basic(token), content=b'{"ID": "x"}'
        )
        assert lock.status_code == 403


async def test_mutating_command_state_token_can_write(client: httpx.AsyncClient) -> None:
    """A mutating command holds apply credentials (§4.3): its `rw` token locks and writes state."""
    from app.statebackend.tokens import decode_state_token

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "cmd-scope-rw-pool")
    stack = await make_stack(client, admin, "cmd-scope-mutating")
    env = await make_env(client, admin, stack, "dev", "dev")
    payload = await _claim_running_command(client, admin, wh, env, "state rm", ["aws_s3_bucket.a"])
    backend = payload["backend"]
    assert backend["lock_address"].endswith(f"/state/v1/{env}/lock")
    token = backend["password"]
    assert decode_state_token(token)["scope"] == "rw"
    body = json.dumps({"ID": "cmd-lock", "Operation": "OperationTypeInvalid"})

    with mock_aws():
        lock = await client.request(
            "LOCK", f"/state/v1/{env}/lock", headers=_basic(token), content=body
        )
        assert lock.status_code == 200, lock.text
        post = await client.post(
            f"/state/v1/{env}?ID=cmd-lock", headers=_basic(token), content=_state_doc(1)
        )
        assert post.status_code == 200, post.text
        unlock = await client.request(
            "UNLOCK", f"/state/v1/{env}/lock", headers=_basic(token), content=body
        )
        assert unlock.status_code == 200

    versions = (
        await client.get(f"/api/v1/environments/{env}/state/versions", headers=admin)
    ).json()
    assert [v["created_by_run_id"] for v in versions] == [payload["job_id"]]


async def _set_managed_state(env_id: str, value: bool) -> None:
    import uuid

    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models.environment import Environment

    async with SessionLocal() as s:
        await s.execute(
            update(Environment)
            .where(Environment.id == uuid.UUID(env_id))
            .values(managed_state=value)
        )
        await s.commit()


async def test_force_unlock_is_admin_only(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")  # approver: can_apply on dev, but not admin
    stack = await make_stack(client, admin, "cmd-unlock-admin")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _set_managed_state(env, False)
    lock_id = "3f2b8c1e-5a4d-4e7f-9b0a-1c2d3e4f5a6b"

    denied = await _command(client, alice, env, "force-unlock", [lock_id])
    assert denied.status_code == 403, denied.text

    ok = await _command(client, admin, env, "force-unlock", [lock_id])
    assert ok.status_code == 201, ok.text
    assert ok.json()["command"] == {"name": "force-unlock", "args": [lock_id]}


async def test_force_unlock_refused_on_managed_state(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "cmd-unlock-managed")
    env = await make_env(client, admin, stack, "dev", "dev")  # managed_state defaults to true
    r = await _command(client, admin, env, "force-unlock", ["abc-123"])
    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert f"/api/v1/environments/{env}/state/lock" in r.json()["detail"]


async def test_force_unlock_args_validated(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "cmd-unlock-args")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _set_managed_state(env, False)
    for args in ([], ["a", "b"], ["-force"], ["-force", "abc"], ["abc def"], ["a" * 200], ["a;b"]):
        r = await _command(client, admin, env, "force-unlock", args)
        assert r.status_code == 400, (args, r.text)


async def test_force_unlock_payload_on_unmanaged_state(client: httpx.AsyncClient) -> None:
    """On an env with its own backend, force-unlock is a mutating command: apply credentials, and
    the lock ID as its only arg — the agent adds `-force` itself (§4.3)."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "cmd-unlock-pool")
    stack = await make_stack(client, admin, "cmd-unlock-payload")
    env = await make_env(client, admin, stack, "dev", "dev")
    await _set_managed_state(env, False)
    run = (await _command(client, admin, env, "force-unlock", ["1712345678901234"])).json()

    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["job_id"] == run["id"]
    assert payload["phase"] == "command"
    assert payload["cred_phase"] == "apply"
    assert payload["backend"] is None  # unmanaged: the repo's own backend
    assert payload["command"] == {"name": "force-unlock", "args": ["1712345678901234"]}

    audit = await client.get(
        "/api/v1/audit", headers=admin, params={"target_kind": "run", "target_id": run["id"]}
    )
    triggered = next(e for e in audit.json() if e["action"] == "run.command_triggered")
    assert triggered["context"]["command"] == "force-unlock"


async def test_command_option_allowlist(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "cmd-options")
    env = await make_env(client, admin, stack, "dev", "dev")

    accepted = [
        ("refresh", ["-target=aws_s3_bucket.a"], ["-target=aws_s3_bucket.a"]),
        (
            "refresh",
            ['-target=module.m["k"].aws_s3_bucket.a[0]', "-target=b.c", "-target=b.c"],
            ['-target=module.m["k"].aws_s3_bucket.a[0]', "-target=b.c"],
        ),
        # Options first: Go's flag parsing stops at the first positional.
        ("state rm", ["aws_s3_bucket.a", "-dry-run"], ["-dry-run", "aws_s3_bucket.a"]),
        ("state mv", ["-dry-run", "a.b", "a.c"], ["-dry-run", "a.b", "a.c"]),
    ]
    for command, args, stored in accepted:
        r = await _command(client, admin, env, command, args)
        assert r.status_code == 201, (command, args, r.text)
        assert r.json()["command"] == {"name": command, "args": stored}
        await client.post(f"/api/v1/runs/{r.json()['id']}/cancel", headers=admin)

    rejected = [
        ("refresh", ["-target"]),  # needs a value
        ("refresh", ["-target=-x"]),
        ("refresh", ["-target=a b"]),
        ("refresh", ["-target=a=b"]),
        ("refresh", ["-target=" + "a" * 600]),
        ("refresh", ["-var=x=1"]),
        ("refresh", ["-var-file=x"]),
        ("refresh", ["-lock=false"]),
        ("refresh", ["-state=other.tfstate"]),
        ("refresh", ["-chdir=/"]),
        ("refresh", ["-backend-config=x"]),
        ("refresh", [f"-target=r.a{i}" for i in range(51)]),
        ("state rm", ["-dry-run=true", "a.b"]),
        ("state rm", ["-target=a.b"]),
        ("state rm", ["-backup=x", "a.b"]),
        ("state mv", ["-lock=false", "a.b", "a.c"]),
        ("output", ["-json"]),
        ("output", ["-raw", "x"]),
        ("show", ["-json"]),
        ("state show", ["-dry-run", "a.b"]),
        ("import", ["-var=x=1", "a.b", "id"]),
        ("taint", ["-allow-missing", "a.b"]),
    ]
    for command, args in rejected:
        r = await _command(client, admin, env, command, args)
        assert r.status_code == 400, (command, args, r.text)
        assert r.headers["content-type"].startswith("application/problem+json")
