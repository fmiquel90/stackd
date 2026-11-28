"""Out-of-band state changes (SPECS §19.7): an unmanaged-state env's state written outside Stackd
between two runs, detected from the serial/lineage the agent reports."""

from __future__ import annotations

import uuid

import httpx

from tests.conftest_phase2 import (
    event,
    login,
    make_env,
    make_proposed_run,
    make_stack,
    register_worker,
)

LINEAGE = "0b9c2f1e-7d4a-4f3e-9a51-6c2b8e0d1a77"
CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _unmanaged_env(client: httpx.AsyncClient, admin: dict, name: str) -> str:
    stack = await make_stack(client, admin, name)
    env = await make_env(client, admin, stack, "dev", "dev")
    r = await client.patch(
        f"/api/v1/environments/{env}", headers=admin, json={"managed_state": False}
    )
    assert r.status_code == 200, r.text
    return env


def _obs(serial: int | None, lineage: str | None = LINEAGE, baseline: bool = False) -> dict:
    return {"serial": serial, "lineage": lineage, "baseline": baseline}


async def _observe(client, wh, run_id: str, *args, **kw) -> httpx.Response:
    return await event(client, wh, run_id, "state_observed", result=_obs(*args, **kw))


async def _env(client, h, env_id: str) -> dict:
    r = await client.get(f"/api/v1/environments/{env_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _plan(client, wh, admin, env_id: str, *, opts: dict | None = None) -> tuple[str, dict]:
    """A tracked run, claimed and planning; returns its id and the claim payload."""
    run = (
        await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json=opts or {})
    ).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    await event(client, wh, run["id"], "phase_started", phase="planning")
    return run["id"], claim.json()


async def _to_applying(client, wh, admin, run_id: str) -> dict:
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    confirm = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert confirm.status_code == 200, confirm.text
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    return claim.json()


async def _finish_apply(client, wh, admin, run_id: str) -> None:
    done = await event(client, wh, run_id, "phase_finished", result={"outputs": {}})
    assert done.status_code == 200, done.text
    final = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
    assert final["state"] == "finished", final


async def _actions(client, admin, env_id: str) -> list[dict]:
    r = await client.get(
        "/api/v1/audit", headers=admin, params={"target_kind": "environment", "target_id": env_id}
    )
    return [e for e in r.json() if e["action"].startswith("environment.out_of_band")]


async def _notifs(client, h) -> list[dict]:
    r = await client.get("/api/v1/notifications", headers=h)
    return [n for n in r.json() if n["kind"] == "out_of_band_change"]


async def test_observe_state_is_asked_of_reviewed_unmanaged_jobs_only(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "oob-claim-pool")
    env = await _unmanaged_env(client, admin, "oob-claim")

    run_id, plan_job = await _plan(client, wh, admin, env)
    assert plan_job["observe_state"] is True
    assert (await _to_applying(client, wh, admin, run_id))["observe_state"] is True
    await _finish_apply(client, wh, admin, run_id)

    # A pull request's plan runs unreviewed code: never observed, and its report is refused.
    pr = await make_proposed_run(env, commit_sha="c" * 40)
    pr_job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert pr_job["job_id"] == pr and pr_job["observe_state"] is False
    await event(client, wh, pr, "phase_started", phase="planning")
    assert (await _observe(client, wh, pr, 9)).status_code == 409
    await event(client, wh, pr, "phase_finished", result={"has_changes": False})

    # A read-only command is not observed; a managed-state env never is.
    ro = await client.post(
        f"/api/v1/environments/{env}/commands", headers=admin, json={"command": "state list"}
    )
    assert ro.status_code == 201, ro.text
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["observe_state"] is False

    stack = await make_stack(client, admin, "oob-claim-managed")
    managed = await make_env(client, admin, stack, "dev", "dev")
    await client.post(f"/api/v1/environments/{managed}/runs", headers=admin, json={})
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["observe_state"] is False


async def test_detection_debounce_and_clearing_by_a_full_apply(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")
    wh = await register_worker(client, admin, "oob-flow-pool")
    env = await _unmanaged_env(client, admin, "oob-flow")

    # First plan: no baseline yet — the observed state is adopted, nothing flagged.
    run1, _ = await _plan(client, wh, admin, env)
    assert (await _observe(client, wh, run1, 5)).status_code == 200
    out = await _env(client, admin, env)
    assert (out["state_serial"], out["state_lineage"]) == (5, LINEAGE)
    assert out["out_of_band_change"] is None and out["state_observed_at"]

    # A plan job never records a baseline: it wrote nothing.
    assert (await _observe(client, wh, run1, 6, baseline=True)).status_code == 409

    # Its apply: a check that matches, then the state Stackd wrote becomes the baseline.
    await _to_applying(client, wh, admin, run1)
    assert (await _observe(client, wh, run1, 5)).status_code == 200
    assert (await _observe(client, wh, run1, 6, baseline=True)).status_code == 200
    await _finish_apply(client, wh, admin, run1)
    out = await _env(client, admin, env)
    assert out["state_serial"] == 6 and out["out_of_band_change"] is None
    assert await _actions(client, admin, env) == []

    # Someone applies by hand (serial 7): the next plan's check flags it.
    run2, _ = await _plan(client, wh, admin, env)
    assert (await _observe(client, wh, run2, 7)).status_code == 200
    out = await _env(client, admin, env)
    change = out["out_of_band_change"]
    assert change["expected_serial"] == 6 and change["observed_serial"] == 7
    assert change["expected_lineage"] == LINEAGE and change["run_id"] == run2
    assert out["state_serial"] == 7  # the baseline moves to what was seen
    [detected] = await _actions(client, admin, env)
    assert detected["action"] == "environment.out_of_band_change_detected"
    assert detected["actor_kind"] == "system"
    assert detected["context"]["run_id"] == run2
    assert detected["context"]["lineage_changed"] is False
    [notif] = await _notifs(client, alice)
    assert notif["run_id"] == run2 and notif["context"]["observed_serial"] == 7
    # The run itself is untouched by the observation.
    assert (await client.get(f"/api/v1/runs/{run2}", headers=admin)).json()["state"] == "planning"

    # Another outside write while flagged: observed moves, expected stays, no new audit/notification.
    assert (await _observe(client, wh, run2, 9)).status_code == 200
    change = (await _env(client, admin, env))["out_of_band_change"]
    assert (change["expected_serial"], change["observed_serial"]) == (6, 9)
    assert len(await _actions(client, admin, env)) == 1
    assert len(await _notifs(client, alice)) == 1

    # A full apply of a plan computed on the changed state clears it.
    await _to_applying(client, wh, admin, run2)
    assert (await _observe(client, wh, run2, 9)).status_code == 200
    assert (await _observe(client, wh, run2, 10, baseline=True)).status_code == 200
    await _finish_apply(client, wh, admin, run2)
    out = await _env(client, admin, env)
    assert out["out_of_band_change"] is None and out["state_serial"] == 10
    cleared = (await _actions(client, admin, env))[0]
    assert cleared["action"] == "environment.out_of_band_change_cleared"
    assert cleared["context"] == {"reason": "applied", "run_id": run2}


async def test_lineage_change_and_emptied_state_are_detected(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "oob-lineage-pool")
    env = await _unmanaged_env(client, admin, "oob-lineage")
    run, _ = await _plan(client, wh, admin, env)

    # An empty state (first deploy) records no baseline.
    assert (await _observe(client, wh, run, None, None)).status_code == 200
    assert (await _env(client, admin, env))["state_lineage"] is None
    assert (await _observe(client, wh, run, 3)).status_code == 200

    other = "11111111-2222-4333-8444-555555555555"
    assert (await _observe(client, wh, run, 3, other)).status_code == 200
    change = (await _env(client, admin, env))["out_of_band_change"]
    assert change["expected_lineage"] == LINEAGE and change["observed_lineage"] == other
    [detected] = await _actions(client, admin, env)
    assert detected["context"]["lineage_changed"] is True

    # A state gone empty is a change too (Stackd never deletes it).
    assert (await _observe(client, wh, run, None, None)).status_code == 200
    change = (await _env(client, admin, env))["out_of_band_change"]
    assert change["observed_serial"] is None and change["observed_lineage"] is None


async def test_malformed_and_stale_observations_are_refused(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "oob-bad-pool")
    env = await _unmanaged_env(client, admin, "oob-bad")
    run, _ = await _plan(client, wh, admin, env)

    for bad in (
        {"serial": "5", "lineage": LINEAGE},
        {"serial": -1, "lineage": LINEAGE},
        {"serial": True, "lineage": LINEAGE},
        {"serial": 5, "lineage": None},
        {"serial": None, "lineage": LINEAGE},
        {"serial": 5, "lineage": "x" * 65},
        {"serial": 5, "lineage": "a b"},
    ):
        r = await event(client, wh, run, "state_observed", result=bad)
        assert r.status_code == 400, (bad, r.text)

    await event(client, wh, run, "phase_finished", result={"has_changes": False})
    # A finished run has no job any more.
    assert (await _observe(client, wh, run, 5)).status_code == 409
    assert (await _env(client, admin, env))["state_serial"] is None


async def test_acknowledge_needs_can_apply_and_clears_the_flag(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    alice = await login(client, "alice")  # approver on dev
    bob = await login(client, "bob")  # writer: cannot apply
    wh = await register_worker(client, admin, "oob-ack-pool")
    env = await _unmanaged_env(client, admin, "oob-ack")
    url = f"/api/v1/environments/{env}/out-of-band-change/acknowledge"

    assert (await client.post(url, headers=alice)).status_code == 409  # nothing flagged
    run, _ = await _plan(client, wh, admin, env)
    await _observe(client, wh, run, 1)
    await _observe(client, wh, run, 4)
    assert (await _env(client, admin, env))["out_of_band_change"] is not None

    assert (await client.post(url, headers=bob)).status_code == 403
    r = await client.post(url, headers=alice)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["out_of_band_change"] is None and out["state_serial"] == 4

    cleared = (await _actions(client, admin, env))[0]
    assert cleared["action"] == "environment.out_of_band_change_cleared"
    assert cleared["actor_email"] == "alice@dev.local"
    assert cleared["context"]["reason"] == "acknowledged"
    assert (cleared["context"]["expected_serial"], cleared["context"]["observed_serial"]) == (1, 4)
    assert (await client.post(url, headers=alice)).status_code == 409


async def test_targeted_apply_moves_the_baseline_but_keeps_the_flag(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "oob-target-pool")
    env = await _unmanaged_env(client, admin, "oob-target")

    run, _ = await _plan(client, wh, admin, env, opts={"targets": ["aws_s3_bucket.a"]})
    await _observe(client, wh, run, 1)
    await _observe(client, wh, run, 2)  # flagged
    await _to_applying(client, wh, admin, run)
    await _observe(client, wh, run, 2)
    await _observe(client, wh, run, 3, baseline=True)
    await _finish_apply(client, wh, admin, run)
    out = await _env(client, admin, env)
    assert out["state_serial"] == 3
    assert out["out_of_band_change"]["observed_serial"] == 2


async def test_backend_change_resets_the_baseline(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.models.environment import Environment

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "oob-reset-pool")
    env = await _unmanaged_env(client, admin, "oob-reset")
    run, _ = await _plan(client, wh, admin, env)
    await _observe(client, wh, run, 1)
    await _observe(client, wh, run, 2)
    await event(client, wh, run, "phase_finished", result={"has_changes": False})

    # Another setting: nothing reset.
    await client.patch(f"/api/v1/environments/{env}", headers=admin, json={"autodeploy": True})
    assert (await _env(client, admin, env))["out_of_band_change"] is not None

    r = await client.patch(
        f"/api/v1/environments/{env}", headers=admin, json={"backend_config": {"key": "other"}}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["out_of_band_change"] is None and out["state_serial"] is None
    async with SessionLocal() as s:
        row = await s.get(Environment, uuid.UUID(env))
        assert row is not None and row.state_lineage is None and row.out_of_band_run_id is None
