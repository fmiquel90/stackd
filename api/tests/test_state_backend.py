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


async def _claim_apply(
    client: httpx.AsyncClient, admin: dict[str, str], wh: dict[str, str], env_id: str
) -> tuple[str, dict]:
    """Trigger a tracked run and take it through its plan job (with changes) and confirmation to
    its apply claim. Returns (run id, apply job payload)."""
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    plan = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert plan["phase"] == "plan"
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result={"has_changes": True})
    confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    assert confirm.status_code == 200, confirm.text
    apply = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert apply["phase"] == "apply"
    return run["id"], apply


def _scope(token: str) -> str:
    from app.statebackend.tokens import decode_state_token

    return decode_state_token(token)["scope"]


async def test_plan_job_token_locks_but_cannot_write_state(client: httpx.AsyncClient) -> None:
    """A plan job's token does what `tofu plan` does (LOCK, GET, UNLOCK) but never POSTs state —
    nor, once its plan phase is over, takes the lock from under that same run's apply (§11.2).
    Only the apply job, which holds apply credentials, writes state."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-plan-pool")
    stack = await make_stack(client, admin, "state-plan-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    plan = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert plan["phase"] == "plan"
    assert plan["backend"]["lock_address"].endswith(f"/state/v1/{env_id}/lock")  # plan locks
    plan_token = plan["backend"]["password"]
    assert _scope(plan_token) == "plan"
    await event(client, wh, run["id"], "phase_started", phase="planning")
    plan_lock = json.dumps({"ID": "plan-lock", "Operation": "OperationTypePlan"})
    apply_lock = json.dumps({"ID": "apply-lock", "Operation": "OperationTypeApply"})

    with mock_aws():
        lock = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(plan_token), content=plan_lock
        )
        assert lock.status_code == 200, lock.text
        got = await client.get(f"/state/v1/{env_id}", headers=_basic(plan_token))
        assert got.status_code == 404  # readable (no state yet)
        post = await client.post(
            f"/state/v1/{env_id}?ID=plan-lock", headers=_basic(plan_token), content=_state_doc(1)
        )
        assert post.status_code == 403  # even holding the lock, a plan never writes state
        unlock = await client.request(
            "UNLOCK", f"/state/v1/{env_id}/lock", headers=_basic(plan_token), content=plan_lock
        )
        assert unlock.status_code == 200

        # Plan done → confirmed → applying. The apply job holds apply credentials and writes.
        await event(client, wh, run["id"], "phase_finished", result={"has_changes": True})
        confirm = await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
        assert confirm.status_code == 200, confirm.text
        apply = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
        assert apply["phase"] == "apply"
        apply_token = apply["backend"]["password"]
        assert _scope(apply_token) == "rw"

        # The (unexpired) plan token can't take the lock during that same run's apply...
        stale_lock = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(plan_token), content=plan_lock
        )
        assert stale_lock.status_code == 403
        assert (
            await client.request(
                "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(apply_token), content=apply_lock
            )
        ).status_code == 200
        # ...nor write state, even with the apply's lock id.
        stale_post = await client.post(
            f"/state/v1/{env_id}?ID=apply-lock", headers=_basic(plan_token), content=_state_doc(1)
        )
        assert stale_post.status_code == 403
        applied = await client.post(
            f"/state/v1/{env_id}?ID=apply-lock", headers=_basic(apply_token), content=_state_doc(1)
        )
        assert applied.status_code == 200, applied.text
        assert (
            await client.request(
                "UNLOCK",
                f"/state/v1/{env_id}/lock",
                headers=_basic(apply_token),
                content=apply_lock,
            )
        ).status_code == 200

    versions = (
        await client.get(f"/api/v1/environments/{env_id}/state/versions", headers=admin)
    ).json()
    assert [v["created_by_run_id"] for v in versions] == [run["id"]]  # only the apply's write


async def test_proposed_run_token_is_read_only_and_never_locks(client: httpx.AsyncClient) -> None:
    """A proposed run (PR plan, drift) only reads state: `ro` token, and no lock address in its
    backend, so `tofu plan` doesn't send the LOCK its token would be refused (§11.2)."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-proposed-pool")
    stack = await make_stack(client, admin, "state-proposed-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (
        await client.post(
            f"/api/v1/environments/{env_id}/runs", headers=admin, json={"type": "proposed"}
        )
    ).json()
    job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert job["job_id"] == run["id"] and job["phase"] == "plan"
    backend = job["backend"]
    assert backend["address"].endswith(f"/state/v1/{env_id}")
    assert not {"lock_address", "unlock_address", "lock_method", "unlock_method"} & set(backend)
    token = backend["password"]
    assert _scope(token) == "ro"
    await event(client, wh, run["id"], "phase_started", phase="planning")
    body = json.dumps({"ID": "pr-lock"})

    with mock_aws():
        assert (await client.get(f"/state/v1/{env_id}", headers=_basic(token))).status_code == 404
        post = await client.post(
            f"/state/v1/{env_id}", headers=_basic(token), content=_state_doc(1)
        )
        assert post.status_code == 403
        lock = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(token), content=body
        )
        assert lock.status_code == 403


async def test_terminal_run_token_cannot_write_state(client: httpx.AsyncClient) -> None:
    """Defense in depth: a run-scoped token writes state / takes the lock only while its run is
    active. Once the run is terminal (e.g. reclaimed as worker_lost), a partitioned worker's
    unexpired token must not write over the next run's state (§4.2)."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-terminal-pool")
    stack = await make_stack(client, admin, "state-terminal-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id, apply = await _claim_apply(client, admin, wh, env_id)
    token = apply["backend"]["password"]  # the apply job: the one that writes state
    lock_body = json.dumps({"ID": "run-lock", "Operation": "OperationTypeApply"})

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
        await event(client, wh, run_id, "job_failed", phase="apply")
        detail = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
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


async def test_unlock_requires_the_holders_lock_id(client: httpx.AsyncClient) -> None:
    """A token-based UNLOCK releases the lock only with the holder's ID (tofu always sends the
    lock info it LOCKed with): no ID or another ID gets 423 + the holder, like a LOCK conflict,
    and leaves the lock in place. The admin force-unlock still breaks any lock."""
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-unlock-id")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    holder = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    other = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    url = f"/state/v1/{env_id}/lock"
    held = json.dumps({"ID": "holder-lock", "Operation": "OperationTypeApply"})

    async def _lock(token: str, body: str) -> httpx.Response:
        return await client.request("LOCK", url, headers=_basic(token), content=body)

    async def _unlock(token: str, body: str) -> httpx.Response:
        return await client.request("UNLOCK", url, headers=_basic(token), content=body)

    # A lock must carry an ID — an ID-less one could only ever be force-unlocked.
    assert (await _lock(holder, json.dumps({"Operation": "OperationTypeApply"}))).status_code == 400
    assert (await _lock(holder, held)).status_code == 200

    for body in ("", "{}", json.dumps({"ID": ""}), json.dumps({"ID": "another-lock"})):
        refused = await _unlock(other, body)
        assert refused.status_code == 423, body
        assert refused.json()["ID"] == "holder-lock"  # the holder, as on a LOCK conflict
    conflict = await _lock(other, json.dumps({"ID": "other-lock"}))
    assert conflict.status_code == 423  # still held by its holder
    assert conflict.json()["ID"] == "holder-lock"

    assert (await _unlock(holder, held)).status_code == 200
    assert (await _unlock(holder, held)).status_code == 200  # nothing held: idempotent
    assert (await _lock(other, json.dumps({"ID": "other-lock"}))).status_code == 200

    # The admin force-unlock (§12) still breaks a lock it doesn't hold the ID of.
    forced = await client.delete(f"/api/v1/environments/{env_id}/state/lock", headers=admin)
    assert forced.status_code == 204
    assert (await _lock(holder, held)).status_code == 200
