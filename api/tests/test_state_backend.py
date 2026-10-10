from __future__ import annotations

import base64
import json
import uuid

import httpx
from moto import mock_aws

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker


def _basic(token: str) -> dict[str, str]:
    raw = base64.b64encode(f"env:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _state_doc(serial: int) -> bytes:
    return json.dumps({"version": 4, "serial": serial, "lineage": "abc", "outputs": {}}).encode()


async def test_state_lock_post_get_unlock(client: httpx.AsyncClient) -> None:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    rw = mint_state_token(
        environment_id=uuid.UUID(env_id), run_id=None, scope="rw", ttl_seconds=3600
    )

    with mock_aws():
        lock_body = json.dumps({"ID": "lock-1", "Operation": "OperationTypePlan"})
        first = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(rw), content=lock_body
        )
        assert first.status_code == 200

        # Second lock holder → 423 with the current holder info.
        second = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(rw), content=lock_body
        )
        assert second.status_code == 423

        post = await client.post(
            f"/state/v1/{env_id}?ID=lock-1", headers=_basic(rw), content=_state_doc(1)
        )
        assert post.status_code == 200

        # Regressive serial refused.
        regressive = await client.post(
            f"/state/v1/{env_id}?ID=lock-1", headers=_basic(rw), content=_state_doc(0)
        )
        assert regressive.status_code == 409

        got = await client.get(f"/state/v1/{env_id}", headers=_basic(rw))
        assert got.status_code == 200
        assert json.loads(got.content)["serial"] == 1

        unlock = await client.request(
            "UNLOCK", f"/state/v1/{env_id}/lock", headers=_basic(rw), content=lock_body
        )
        assert unlock.status_code == 200


async def test_readonly_token_cannot_write(client: httpx.AsyncClient) -> None:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-ro-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    ro = mint_state_token(
        environment_id=uuid.UUID(env_id), run_id=None, scope="ro", ttl_seconds=3600
    )
    with mock_aws():
        post = await client.post(f"/state/v1/{env_id}", headers=_basic(ro), content=_state_doc(1))
        assert post.status_code == 403  # proposed runs get read-only state (§13)


async def test_state_token_scoped_to_env(client: httpx.AsyncClient) -> None:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-scope-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    other = mint_state_token(environment_id=uuid.uuid4(), run_id=None, scope="rw", ttl_seconds=3600)
    got = await client.get(f"/state/v1/{env_id}", headers=_basic(other))
    assert got.status_code == 403


async def _set_managed_state(env_id: str, value: bool) -> None:
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


async def test_import_session_adopts_existing_state(client: httpx.AsyncClient) -> None:
    """Adopt an existing stack: mint an import session, then migrate state with the returned
    backend config (the LOCK/POST/UNLOCK that `tofu init -migrate-state` performs)."""
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-import-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")  # managed_state defaults to true

    r = await client.post(f"/api/v1/environments/{env_id}/state/import-session", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_serial"] is None
    assert body["backend"]["type"] == "http"
    assert body["backend"]["address"].endswith(f"/state/v1/{env_id}")
    assert body["instructions"]
    token = body["backend"]["password"]

    with mock_aws():
        lock_body = json.dumps({"ID": "import-1", "Operation": "OperationTypeMigrate"})
        assert (
            await client.request(
                "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(token), content=lock_body
            )
        ).status_code == 200
        post = await client.post(
            f"/state/v1/{env_id}?ID=import-1", headers=_basic(token), content=_state_doc(7)
        )
        assert post.status_code == 200
        assert (
            await client.request(
                "UNLOCK", f"/state/v1/{env_id}/lock", headers=_basic(token), content=lock_body
            )
        ).status_code == 200

    versions = (
        await client.get(f"/api/v1/environments/{env_id}/state/versions", headers=admin)
    ).json()
    assert len(versions) == 1
    assert versions[0]["serial"] == 7
    assert versions[0]["created_by_run_id"] is None  # imported out of any run


async def test_readonly_token_cannot_unlock(client: httpx.AsyncClient) -> None:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-unlock-rbac")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    rw = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    ro = mint_state_token(environment_id=uuid.UUID(env_id), scope="ro", ttl_seconds=3600)
    body = json.dumps({"ID": "lk-1"})

    assert (
        await client.request("LOCK", f"/state/v1/{env_id}/lock", headers=_basic(rw), content=body)
    ).status_code == 200
    # A read-only token never locks, so it must not be able to unlock either.
    denied = await client.request(
        "UNLOCK", f"/state/v1/{env_id}/lock", headers=_basic(ro), content=body
    )
    assert denied.status_code == 403


async def test_import_session_requires_admin(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")
    stack = await make_stack(client, admin, "state-import-rbac")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    r = await client.post(f"/api/v1/environments/{env_id}/state/import-session", headers=bob)
    assert r.status_code == 403


async def test_import_session_requires_managed_state(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-import-unmanaged")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    await _set_managed_state(env_id, False)
    r = await client.post(f"/api/v1/environments/{env_id}/state/import-session", headers=admin)
    assert r.status_code == 409


def _ttl(token: str) -> int:
    from app.statebackend.tokens import decode_state_token

    claims = decode_state_token(token)
    return claims["exp"] - claims["iat"]


async def test_state_token_ttl_follows_job_credential_phase(client: httpx.AsyncClient) -> None:
    """A plan job keeps the generous state-token window; the apply job of the same run gets the
    apply window (budget + grace), the same as its OIDC cap and worker-lost reclaim cutoff."""
    from app.config import get_settings

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-ttl-pool")
    stack = await make_stack(client, admin, "state-ttl-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()

    plan = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert plan["phase"] == "plan"
    assert _ttl(plan["backend"]["password"]) == 6 * 3600

    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result={"has_changes": True})
    confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert confirm.status_code == 200, confirm.text

    apply = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert apply["phase"] == "apply"
    assert _ttl(apply["backend"]["password"]) == get_settings().apply_reclaim_window_seconds


async def test_terminal_run_token_cannot_write_state(client: httpx.AsyncClient) -> None:
    """Defense in depth: a run-scoped token writes state / takes the lock only while its run is
    active. Once the run is terminal (e.g. reclaimed as worker_lost), a partitioned worker's
    unexpired token must not write over the next run's state (§4.2)."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-terminal-pool")
    stack = await make_stack(client, admin, "state-terminal-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    token = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["backend"]["password"]
    lock_body = json.dumps({"ID": "run-lock", "Operation": "OperationTypePlan"})

    with mock_aws():
        # Active run: the legitimate LOCK / POST / UNLOCK still work.
        lock = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(token), content=lock_body
        )
        assert lock.status_code == 200
        post = await client.post(
            f"/state/v1/{env_id}?ID=run-lock", headers=_basic(token), content=_state_doc(1)
        )
        assert post.status_code == 200, post.text
        unlock = await client.request(
            "UNLOCK", f"/state/v1/{env_id}/lock", headers=_basic(token), content=lock_body
        )
        assert unlock.status_code == 200

        # The run ends (through transition(), like a worker_lost reclaim): its token, though
        # unexpired, can no longer write state nor take the lock.
        await event(client, wh, run["id"], "job_failed", phase="planning")
        detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
        assert detail["state"] == "failed"

        stale_post = await client.post(
            f"/state/v1/{env_id}", headers=_basic(token), content=_state_doc(2)
        )
        assert stale_post.status_code == 403
        stale_lock = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(token), content=lock_body
        )
        assert stale_lock.status_code == 403
        # Reads are unaffected, and the refused write left no new version behind.
        got = await client.get(f"/state/v1/{env_id}", headers=_basic(token))
        assert got.status_code == 200
        assert json.loads(got.content)["serial"] == 1
