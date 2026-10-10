"""The saved plan (SPECS §7.4/§11.3): the plan job uploads its plan file, the API stores it
AES-256-GCM-encrypted in S3, serves it only to the run's apply job, and deletes it once the run is
terminal. The apply applies exactly it — never a fresh plan."""

from __future__ import annotations

import httpx
from moto import mock_aws

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

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


async def _stored(run_id: str) -> bytes | None:
    from app.statebackend.store import get_object

    return await get_object(f"artifacts/{run_id}/plan.tfplan")


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

        key = f"artifacts/{run_id}/plan.tfplan"  # §11.3 layout
        blob = await _stored(run_id)
        assert blob is not None and await _plan_key(run_id) == key
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

        key_a = f"artifacts/{a}/plan.tfplan"
        await put_object(
            f"artifacts/{b}/plan.tfplan",
            encrypt_bytes(b"PK evil", aad=f"stackd:artifact:{key_a}".encode()),
        )
        await _confirm_and_claim_apply(client, admin, wh, b)
        got = await client.get(_url(b), headers=wh)
        assert got.status_code == 500, got.text
        assert got.json()["title"] == "Saved plan unreadable"


async def test_proposed_run_saves_no_plan(client: httpx.AsyncClient) -> None:
    admin, wh, env_id = await _setup(client, "sp-proposed")
    with mock_aws():
        run = await client.post(
            f"/api/v1/environments/{env_id}/runs", headers=admin, json={"type": "proposed"}
        )
        job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
        assert job["save_plan"] is False
        run_id = run.json()["id"]
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
        assert await _plan_key(run_id) == f"artifacts/{run_id}/plan.tfplan"
