"""The saved plan (SPECS §7.4/§11.3): the plan job uploads its plan file, the API stores it
AES-256-GCM-encrypted in S3, serves it only to the run's apply job, and deletes it once the run is
terminal. The apply applies exactly it — never a fresh plan."""

from __future__ import annotations

import re

import httpx
from moto import mock_aws

from tests.conftest_phase2 import (
    event,
    login,
    make_env,
    make_proposed_run,
    make_stack,
    register_worker,
)

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}
SHA = "c0ffee" + "0" * 34
# A plan file holds the run's variable values in clear, sensitive ones included.
PLAN = b"PK\x03\x04 tfplan ... db_password=SUPER-SECRET-VALUE ... \x00\xff"


def _url(run_id: str) -> str:
    return f"/worker/v1/jobs/{run_id}/artifacts/plan.tfplan"


async def _setup(client: httpx.AsyncClient, name: str) -> tuple[dict, dict, str]:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-{name}")
    stack = await make_stack(client, admin, f"{name}-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    return admin, wh, env_id


async def _plan(client, admin, wh, env_id, *, run_type: str = "tracked", upload: bool = True):
    """Trigger + claim + plan (uploading PLAN) → unconfirmed. Returns (run id, plan-claim payload)."""
    run = await client.post(
        f"/api/v1/environments/{env_id}/runs", headers=admin, json={"type": run_type}
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert job["job_id"] == run_id
    await event(client, wh, run_id, "phase_started", phase="planning", result={"commit_sha": SHA})
    if upload:
        put = await client.put(_url(run_id), headers=wh, content=PLAN)
        assert put.status_code == 204, put.text
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    return run_id, job


async def _confirm_and_claim_apply(client, admin, wh, run_id: str) -> dict:
    confirm = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert confirm.status_code == 200, confirm.text
    job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert (job["job_id"], job["phase"]) == (run_id, "apply")
    return job


async def _objects(run_id: str) -> list[str]:
    from app.statebackend.store import list_keys

    return await list_keys(f"artifacts/{run_id}/")


async def _stored(run_id: str) -> bytes | None:
    from app.statebackend.store import get_object

    keys = await _objects(run_id)
    assert len(keys) <= 1, keys
    return await get_object(keys[0]) if keys else None


async def _plan_key(run_id: str) -> str | None:
    from app.db import SessionLocal
    from app.models.run import Run

    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        assert run is not None
        return run.plan_artifact_key


async def test_plan_is_saved_encrypted_and_served_to_the_apply_job(
    client: httpx.AsyncClient,
) -> None:
    from app.crypto import decrypt_bytes

    admin, wh, env_id = await _setup(client, "sp-roundtrip")
    with mock_aws():
        run_id, plan_job = await _plan(client, admin, wh, env_id)
        assert (plan_job["save_plan"], plan_job["destroy"]) == (True, False)

        key = await _plan_key(run_id)
        assert key is not None and re.fullmatch(rf"artifacts/{run_id}/plan-[0-9a-f-]+\.tfplan", key)
        blob = await _stored(run_id)  # §11.3 layout
        assert blob is not None and await _objects(run_id) == [key]
        assert b"SUPER-SECRET-VALUE" not in blob  # encrypted at rest (AES-256-GCM)…
        assert decrypt_bytes(blob, aad=f"stackd:artifact:{key}".encode()) == PLAN  # …app key

        # Awaiting confirmation: nobody downloads it yet, not even the worker that planned it.
        early = await client.get(_url(run_id), headers=wh)
        assert early.status_code == 409, early.text

        apply_job = await _confirm_and_claim_apply(client, admin, wh, run_id)
        assert apply_job["save_plan"] is False
        assert apply_job["environment"]["commit_sha"] == SHA
        got = await client.get(_url(run_id), headers=wh)
        assert got.status_code == 200, got.text
        assert got.content == PLAN  # byte for byte what the plan job uploaded


async def test_reviewed_plan_cannot_be_replaced(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "sp-frozen")
    with mock_aws():
        run_id, _ = await _plan(client, admin, wh, env_id)
        swap = await client.put(_url(run_id), headers=wh, content=b"PK another plan")
        assert swap.status_code == 409, swap.text  # unconfirmed: the plan job is over

        await _confirm_and_claim_apply(client, admin, wh, run_id)
        swap = await client.put(_url(run_id), headers=wh, content=b"PK another plan")
        assert swap.status_code == 409, swap.text  # the apply job can't swap it either
        assert (await client.get(_url(run_id), headers=wh)).content == PLAN


async def test_only_the_runs_apply_worker_gets_the_plan(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "sp-owner")
    other = await register_worker(client, admin, "pool-sp-owner-2", "w2")
    with mock_aws():
        run_id, _ = await _plan(client, admin, wh, env_id)
        await _confirm_and_claim_apply(client, admin, wh, run_id)
        stolen = await client.get(_url(run_id), headers=other)
        assert stolen.status_code == 409, stolen.text


async def test_apply_without_a_saved_plan_gets_no_plan(client: httpx.AsyncClient) -> None:
    # Fail closed: nothing else is ever served in its place (the worker then fails the apply).
    admin, wh, env_id = await _setup(client, "sp-missing")
    with mock_aws():
        run_id, _ = await _plan(client, admin, wh, env_id, upload=False)
        await _confirm_and_claim_apply(client, admin, wh, run_id)
        got = await client.get(_url(run_id), headers=wh)
        assert got.status_code == 404, got.text
        assert "no saved plan" in got.json()["detail"].lower()


async def test_a_plan_moved_to_another_run_does_not_decrypt(client: httpx.AsyncClient) -> None:
    from app.statebackend.store import put_object

    admin, wh, env_id = await _setup(client, "sp-swap")
    with mock_aws():
        a, _ = await _plan(client, admin, wh, env_id)
        await client.post(f"/api/v1/runs/{a}/discard", headers=admin)
        b, _ = await _plan(client, admin, wh, env_id)
        # Run b's object overwritten with a blob encrypted for another key (here: a's plan).
        from app.crypto import encrypt_bytes

        key_a = f"artifacts/{a}/plan-0.tfplan"
        key_b = await _plan_key(b)
        assert key_b is not None
        await put_object(key_b, encrypt_bytes(b"PK evil", aad=f"stackd:artifact:{key_a}".encode()))
        await _confirm_and_claim_apply(client, admin, wh, b)
        got = await client.get(_url(b), headers=wh)
        assert got.status_code == 500, got.text
        assert got.json()["title"] == "Saved plan unreadable"


async def test_proposed_run_saves_no_plan(client: httpx.AsyncClient) -> None:
    _admin, wh, env_id = await _setup(client, "sp-proposed")
    with mock_aws():
        run_id = await make_proposed_run(env_id)
        job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
        assert job["save_plan"] is False
        await event(client, wh, run_id, "phase_started", phase="planning")
        put = await client.put(_url(run_id), headers=wh, content=PLAN)
        assert put.status_code == 409, put.text
        assert await _stored(run_id) is None


async def test_destroy_run_plans_with_destroy_and_saves_its_plan(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "sp-destroy")
    with mock_aws():
        _, job = await _plan(client, admin, wh, env_id, run_type="destroy")
        assert (job["destroy"], job["save_plan"]) == (True, True)


async def test_plan_upload_is_size_capped(client: httpx.AsyncClient, monkeypatch) -> None:
    import app.workers.worker_router as wr

    admin, wh, env_id = await _setup(client, "sp-cap")
    monkeypatch.setattr(wr, "MAX_PLAN_BYTES", 1024)
    with mock_aws():
        run = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
        run_id = run.json()["id"]
        await client.post("/worker/v1/jobs/claim", headers=wh)
        too_big = await client.put(_url(run_id), headers=wh, content=b"x" * 1025)
        assert too_big.status_code == 413, too_big.text

        async def chunks():  # no Content-Length: the cap holds while streaming too
            for _ in range(3):
                yield b"x" * 512

        streamed = await client.put(_url(run_id), headers=wh, content=chunks())
        assert streamed.status_code == 413, streamed.text
        empty = await client.put(_url(run_id), headers=wh, content=b"")
        assert empty.status_code == 400, empty.text
        assert await _stored(run_id) is None


async def test_saved_plan_is_deleted_once_the_run_is_terminal(client: httpx.AsyncClient) -> None:
    from app.runs.artifacts import wait_for_purges

    admin, wh, env_id = await _setup(client, "sp-retention")
    with mock_aws():
        # Applied → finished.
        applied, _ = await _plan(client, admin, wh, env_id)
        await _confirm_and_claim_apply(client, admin, wh, applied)
        await event(client, wh, applied, "phase_finished")
        # Discarded while awaiting confirmation.
        discarded, _ = await _plan(client, admin, wh, env_id)
        assert await _stored(discarded) is not None
        await client.post(f"/api/v1/runs/{discarded}/discard", headers=admin)
        # Apply failed (e.g. tofu refused a stale plan).
        failed, _ = await _plan(client, admin, wh, env_id)
        await _confirm_and_claim_apply(client, admin, wh, failed)
        await event(client, wh, failed, "job_failed", phase="apply", result={"error": "stale"})

        await wait_for_purges()
        for run_id in (applied, discarded, failed):
            assert await _stored(run_id) is None, run_id
            assert await _plan_key(run_id) is None, run_id


async def test_a_rolled_back_terminal_transition_keeps_the_plan(client: httpx.AsyncClient) -> None:
    # The S3 delete only follows a committed terminal transition.
    from app.db import SessionLocal
    from app.enums import RunEventActor, RunState
    from app.models.run import Run
    from app.runs.artifacts import wait_for_purges
    from app.runs.transition import transition

    admin, wh, env_id = await _setup(client, "sp-rollback")
    with mock_aws():
        run_id, _ = await _plan(client, admin, wh, env_id)
        key = await _plan_key(run_id)
        async with SessionLocal() as s:
            run = await s.get(Run, run_id)
            assert run is not None
            await transition(s, run, RunState.discarded, actor=RunEventActor.system)
            assert run.plan_artifact_key is None
            await s.rollback()
            await s.get(Run, run_id)  # a later, unrelated transaction of the same session commits
            await s.commit()
        await wait_for_purges()
        assert await _stored(run_id) is not None
        assert key is not None and await _plan_key(run_id) == key


# --- errored state (§7.3): what an apply could not write back is kept, encrypted --------------

ERRORED = b'{"version": 4, "serial": 7, "resources": [{"db_password": "SUPER-SECRET-VALUE"}]}'


def _errored_url(run_id: str) -> str:
    return f"/worker/v1/jobs/{run_id}/artifacts/errored.tfstate"


async def test_errored_state_is_stored_encrypted_audited_and_kept(
    client: httpx.AsyncClient,
) -> None:
    from sqlalchemy import select

    from app.crypto import decrypt_bytes
    from app.db import SessionLocal
    from app.models.audit import AuditEvent
    from app.runs.artifacts import wait_for_purges
    from app.statebackend.store import get_object

    admin, wh, env_id = await _setup(client, "sp-errored")
    with mock_aws():
        run_id, _ = await _plan(client, admin, wh, env_id)
        # The plan job holds no write credential: it never sends one.
        early = await client.put(_errored_url(run_id), headers=wh, content=ERRORED)
        assert early.status_code == 409, early.text

        await _confirm_and_claim_apply(client, admin, wh, run_id)
        put = await client.put(_errored_url(run_id), headers=wh, content=ERRORED)
        assert put.status_code == 204, put.text
        await event(client, wh, run_id, "job_failed", phase="apply", result={"error": "401"})
        await wait_for_purges()

        key = f"artifacts/{run_id}/errored.tfstate"
        blob = await get_object(key)
        assert blob is not None, "kept once the run is terminal, unlike the saved plan"
        assert b"SUPER-SECRET-VALUE" not in blob
        assert decrypt_bytes(blob, aad=f"stackd:artifact:{key}".encode()) == ERRORED
        async with SessionLocal() as s:
            audits = (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.action == "run.errored_state_saved")
                )
            ).scalars()
            audit = next(a for a in audits if str(a.target_id) == run_id)
        assert audit.context == {"environment_id": env_id, "key": key, "size_bytes": len(ERRORED)}

        # Terminal now: a late upload is refused (the agent then keeps the file on the worker).
        late = await client.put(_errored_url(run_id), headers=wh, content=ERRORED)
        assert late.status_code == 409, late.text


async def test_errored_state_only_from_the_runs_worker(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "sp-errored-owner")
    other = await register_worker(client, admin, "pool-sp-errored-owner-2", "w2")
    with mock_aws():
        run_id, _ = await _plan(client, admin, wh, env_id)
        await _confirm_and_claim_apply(client, admin, wh, run_id)
        stolen = await client.put(_errored_url(run_id), headers=other, content=ERRORED)
        assert stolen.status_code == 409, stolen.text
        empty = await client.put(_errored_url(run_id), headers=wh, content=b"")
        assert empty.status_code == 400, empty.text
