from __future__ import annotations

import base64
import json
import uuid

import httpx
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tests.conftest_phase2 import (
    event,
    login,
    make_env,
    make_proposed_run,
    make_stack,
    register_worker,
)


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
    run = {"id": await make_proposed_run(env_id)}
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


async def test_post_requires_the_holders_lock_id_while_locked(client: httpx.AsyncClient) -> None:
    """While the state is locked, only a POST naming the held lock (`?ID=`, as tofu sends when it
    holds one) writes: no ID or another one gets 423 + the holder, like a LOCK conflict, and writes
    nothing. Unlocked, an ID-less POST still writes (`tofu -lock=false`)."""
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-post-lock-id")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    holder = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    other = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    held = json.dumps({"ID": "holder-lock", "Operation": "OperationTypeApply"})

    async def _serials() -> list[int]:
        versions = await client.get(f"/api/v1/environments/{env_id}/state/versions", headers=admin)
        return [v["serial"] for v in versions.json()]

    with mock_aws():
        unlocked = await client.post(
            f"/state/v1/{env_id}", headers=_basic(other), content=_state_doc(1)
        )
        assert unlocked.status_code == 200  # no lock held: `-lock=false` writes
        lock = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(holder), content=held
        )
        assert lock.status_code == 200

        for query in ("", "?ID=", "?ID=another-lock"):
            refused = await client.post(
                f"/state/v1/{env_id}{query}", headers=_basic(other), content=_state_doc(2)
            )
            assert refused.status_code == 423, query
            assert refused.json()["ID"] == "holder-lock"  # the holder, as on a LOCK conflict
        assert await _serials() == [1]

        written = await client.post(
            f"/state/v1/{env_id}?ID=holder-lock", headers=_basic(holder), content=_state_doc(2)
        )
        assert written.status_code == 200
        assert await _serials() == [2, 1]


async def _lock_row(env_id: str) -> tuple[str, str | None] | None:
    """The env's state lock as (lock_id, holder run id), or None."""
    from app.db import SessionLocal
    from app.models.state import StateLock

    async with SessionLocal() as s:
        lock = await s.get(StateLock, uuid.UUID(env_id))
        return None if lock is None else (lock.lock_id, str(lock.run_id) if lock.run_id else None)


async def _lock_releases(env_id: str) -> list[dict]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.audit import AuditEvent

    async with SessionLocal() as s:
        rows = await s.execute(
            select(AuditEvent).where(
                AuditEvent.action == "state.lock_released",
                AuditEvent.target_id == uuid.UUID(env_id),
            )
        )
        return [{"actor_kind": e.actor_kind.value, **(e.context or {})} for e in rows.scalars()]


async def _lose_worker(run_id: str, *, claimed_ago_seconds: int) -> int:
    """Mark the run's worker long offline, backdate its claim, run the worker-lost detector."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select, update

    from app.db import SessionLocal
    from app.models.run import Run
    from app.models.worker import Worker
    from app.scheduler.tasks import detect_worker_lost

    now = datetime.now(UTC)
    run = uuid.UUID(run_id)
    async with SessionLocal() as s:
        worker = select(Run.worker_id).where(Run.id == run).scalar_subquery()
        await s.execute(
            update(Worker)
            .where(Worker.id == worker)
            .values(last_heartbeat_at=now - timedelta(hours=1))
        )
        await s.execute(
            update(Run)
            .where(Run.id == run)
            .values(claimed_at=now - timedelta(seconds=claimed_ago_seconds))
        )
        await s.commit()
    async with SessionLocal() as s:
        return await detect_worker_lost(s, datetime.now(UTC))


async def test_lost_plan_releases_its_state_lock(client: httpx.AsyncClient) -> None:
    """A worker lost mid-plan never UNLOCKs: the lock its run took is released when worker_lost
    fails the run (same transaction, audited), so the env's next run can lock again instead of
    failing with 423 until an admin force-unlocks (§11.2)."""
    from app.config import get_settings
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-lost-plan-pool")
    stack = await make_stack(client, admin, "state-lost-plan")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    plan = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    token = plan["backend"]["password"]
    await event(client, wh, run["id"], "phase_started", phase="planning")
    url = f"/state/v1/{env_id}/lock"
    held = json.dumps({"ID": "lost-plan-lock", "Operation": "OperationTypePlan"})

    locked = await client.request("LOCK", url, headers=_basic(token), content=held)
    assert locked.status_code == 200
    assert await _lock_row(env_id) == ("lost-plan-lock", run["id"])

    lost = await _lose_worker(
        run["id"], claimed_ago_seconds=get_settings().stackd_worker_lost_seconds + 60
    )
    assert lost == 1
    detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert (detail["state"], detail["error"]) == ("failed", "worker_lost")
    assert await _lock_row(env_id) is None
    assert await _lock_releases(env_id) == [
        {
            "actor_kind": "system",
            "reason": "run_ended",
            "run_id": run["id"],
            "run_state": "failed",
            "lock_id": "lost-plan-lock",
        }
    ]

    # The env is free again; the lost holder's late UNLOCK can't free the new holder's lock.
    other = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    nxt = json.dumps({"ID": "next-lock"})
    assert (
        await client.request("LOCK", url, headers=_basic(other), content=nxt)
    ).status_code == 200
    late = await client.request("UNLOCK", url, headers=_basic(token), content=held)
    assert late.status_code == 423
    assert await _lock_row(env_id) == ("next-lock", None)


async def test_lost_apply_releases_its_lock_only_after_the_apply_window(
    client: httpx.AsyncClient,
) -> None:
    """An apply's lock is released only when its run is reclaimed — after the apply window, when
    its `rw` token has expired — never while a healthy long apply may still be writing state."""
    from app.config import get_settings

    settings = get_settings()
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-lost-apply-pool")
    stack = await make_stack(client, admin, "state-lost-apply")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id, apply = await _claim_apply(client, admin, wh, env_id)
    token = apply["backend"]["password"]
    await event(client, wh, run_id, "phase_started", phase="applying")
    held = json.dumps({"ID": "apply-lock", "Operation": "OperationTypeApply"})
    locked = await client.request(
        "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(token), content=held
    )
    assert locked.status_code == 200

    inside = await _lose_worker(
        run_id, claimed_ago_seconds=settings.stackd_worker_lost_seconds + 60
    )
    assert inside == 0
    assert await _lock_row(env_id) == ("apply-lock", run_id)
    assert await _lock_releases(env_id) == []

    past = await _lose_worker(
        run_id, claimed_ago_seconds=settings.apply_reclaim_window_seconds + 60
    )
    assert past == 1
    assert await _lock_row(env_id) is None
    [released] = await _lock_releases(env_id)
    assert (released["run_id"], released["lock_id"]) == (run_id, "apply-lock")


async def test_run_end_keeps_a_lock_it_does_not_hold(client: httpx.AsyncClient) -> None:
    """A run ending releases only its own lock: one taken by a run-less token (import session) or
    recorded for another run stays, for its holder or the admin force-unlock."""
    from app.db import SessionLocal
    from app.models.state import StateLock
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "state-keep-lock-pool")
    stack = await make_stack(client, admin, "state-keep-lock")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    url = f"/state/v1/{env_id}/lock"

    async def _failed_run() -> str:
        run = (
            await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
        ).json()
        assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
        await event(client, wh, run["id"], "phase_started", phase="planning")
        await event(client, wh, run["id"], "job_failed", phase="plan")
        detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
        assert detail["state"] == "failed"
        return str(run["id"])

    runless = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    held = json.dumps({"ID": "import-lock", "Operation": "OperationTypeMigrate"})
    assert (
        await client.request("LOCK", url, headers=_basic(runless), content=held)
    ).status_code == 200
    await _failed_run()
    assert await _lock_row(env_id) == ("import-lock", None)
    unlocked = await client.request("UNLOCK", url, headers=_basic(runless), content=held)
    assert unlocked.status_code == 200

    earlier = await _failed_run()
    async with SessionLocal() as s:
        s.add(
            StateLock(
                environment_id=uuid.UUID(env_id), lock_id="earlier", run_id=uuid.UUID(earlier)
            )
        )
        await s.commit()
    await _failed_run()
    assert await _lock_row(env_id) == ("earlier", earlier)
    assert await _lock_releases(env_id) == []


async def test_state_lock_is_readable_with_its_holder_run(client: httpx.AsyncClient) -> None:
    """`GET …/state/lock` shows the held lock (§11.2): its holder run and tofu's lock info, so an
    admin sees who holds it before a force-unlock; null once nothing is held."""
    admin = await login(client, "admin")
    bob = await login(client, "bob")
    wh = await register_worker(client, admin, "state-lock-read-pool")
    stack = await make_stack(client, admin, "state-lock-read")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    url = f"/api/v1/environments/{env_id}/state/lock"
    assert (await client.get(url, headers=admin)).json() is None

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    plan = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    await event(client, wh, run["id"], "phase_started", phase="planning")
    info = {
        "ID": "read-lock",
        "Operation": "OperationTypePlan",
        "Info": "",
        "Who": "runner@worker-1",
        "Version": "1.12.0",
        "Created": "2026-11-27T10:00:00.000000Z",
        "Path": "",
    }
    locked = await client.request(
        "LOCK",
        f"/state/v1/{env_id}/lock",
        headers=_basic(plan["backend"]["password"]),
        content=json.dumps(info),
    )
    assert locked.status_code == 200

    for headers in (admin, bob):  # any member reads it; the force-unlock stays admin-only
        got = await client.get(url, headers=headers)
        assert got.status_code == 200, got.text
        body = got.json()
        assert body.pop("locked_at")
        assert body == {
            "lock_id": "read-lock",
            "run_id": run["id"],
            "operation": "OperationTypePlan",
            "who": "runner@worker-1",
            "version": "1.12.0",
            "created": "2026-11-27T10:00:00.000000Z",
            "info": None,
        }

    assert (await client.delete(url, headers=bob)).status_code == 403
    assert (await client.delete(url, headers=admin)).status_code == 204
    assert (await client.get(url, headers=admin)).json() is None


def test_created_bucket_gets_versioning_and_encryption() -> None:
    """§11.3: a bucket the API creates has versioning ON and default encryption."""
    from app.statebackend.store import _client, _ensure_bucket

    with mock_aws():
        client = _client()
        _ensure_bucket(client, "stackd-hardened")
        assert client.get_bucket_versioning(Bucket="stackd-hardened")["Status"] == "Enabled"
        config = client.get_bucket_encryption(Bucket="stackd-hardened")
        rule = config["ServerSideEncryptionConfiguration"]["Rules"][0]
        assert rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


class _NoBucketSettings:
    """An S3 without the versioning/encryption calls, like Garage in dev."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def head_bucket(self, **_: object) -> None:
        raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def create_bucket(self, Bucket: str) -> None:  # boto3 casing
        self.created.append(Bucket)

    def put_bucket_versioning(self, **_: object) -> None:
        raise ClientError({"Error": {"Code": "NotImplemented"}}, "PutBucketVersioning")

    def put_bucket_encryption(self, **_: object) -> None:
        raise ClientError({"Error": {"Code": "NotImplemented"}}, "PutBucketEncryption")


def test_unsupported_bucket_settings_only_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.statebackend import store

    # Record the warnings directly: Alembic's fileConfig may have disabled the existing loggers.
    warned: list[str] = []
    monkeypatch.setattr(store._log, "warning", lambda *_a, extra: warned.append(extra["setting"]))
    client = _NoBucketSettings()
    store._ensure_bucket(client, "garage-bucket")
    assert client.created == ["garage-bucket"]
    assert warned == ["versioning", "encryption"]


def test_s3_client_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Static keys when set; in production without keys, the AWS default credential chain (no key
    passed to boto3) — instance profile, task role, IRSA; dev keeps its placeholder keys."""
    from types import SimpleNamespace

    from app.statebackend import store

    seen: list[dict] = []

    class _Session:
        def client(self, *_a: object, **kw: object) -> None:
            seen.append(kw)

    monkeypatch.setattr(store.boto3.session, "Session", _Session)

    def settings(**kw):  # type: ignore[no-untyped-def]
        base = {
            "s3_access_key": "",
            "s3_secret_key": "",
            "is_production": False,
            "s3_endpoint_url": None,
            "aws_region": "eu-west-1",
        }
        return SimpleNamespace(**{**base, **kw})

    monkeypatch.setattr(store, "get_settings", lambda: settings(is_production=True))
    store._build_client()
    assert "aws_access_key_id" not in seen[-1]
    monkeypatch.setattr(
        store,
        "get_settings",
        lambda: settings(is_production=True, s3_access_key="AK", s3_secret_key="SK"),
    )
    store._build_client()
    assert seen[-1]["aws_access_key_id"] == "AK"
    monkeypatch.setattr(store, "get_settings", lambda: settings())
    store._build_client()
    assert seen[-1]["aws_access_key_id"] == "test"
