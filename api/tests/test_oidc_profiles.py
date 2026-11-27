"""Named AWS profiles of a cloud integration (SPECS §10.6): validated as one model on the route,
audited by name, and handed to the job with the role of its credential phase."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models.audit import AuditEvent
from tests.conftest_phase2 import login, make_env, make_stack, register_worker

PLAN_ROLE = "arn:aws:iam::123456789012:role/stackd-plan"
APPLY_ROLE = "arn:aws:iam::123456789012:role/stackd-apply"
STATE_PLAN = "arn:aws:iam::210987654321:role/tfstates-plan"
STATE_APPLY = "arn:aws:iam::210987654321:role/tfstates-apply"
ADMIN_PLAN = "arn:aws-us-gov:iam::333333333333:role/path/prod-admin-plan"
ADMIN_APPLY = "arn:aws-us-gov:iam::333333333333:role/path/prod-admin-apply"
PROFILES = {
    "x-tfstates": {"plan_role_arn": STATE_PLAN, "apply_role_arn": STATE_APPLY},
    "x-prod.admin": {
        "plan_role_arn": ADMIN_PLAN,
        "apply_role_arn": ADMIN_APPLY,
        "region": "us-gov-west-1",
    },
}


async def _put(client: httpx.AsyncClient, admin: dict, env_id: str, **extra: object):
    return await client.put(
        f"/api/v1/environments/{env_id}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": APPLY_ROLE, **extra},
    )


async def test_profiles_are_stored_returned_and_audited(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "profiles-crud")
    env_id = await make_env(client, admin, stack, "dev", "dev")

    r = await _put(client, admin, env_id, profiles=PROFILES)
    assert r.status_code == 200, r.text
    expected = {
        "x-prod.admin": PROFILES["x-prod.admin"],
        "x-tfstates": {**PROFILES["x-tfstates"], "region": None},
    }
    assert r.json()["profiles"] == expected
    got = await client.get(f"/api/v1/environments/{env_id}/cloud-integration", headers=admin)
    assert got.json()["profiles"] == expected
    assert list(got.json()["profiles"]) == ["x-prod.admin", "x-tfstates"]  # sorted, stable

    # A PUT replaces the whole set; no `profiles` → none.
    r = await _put(client, admin, env_id)
    assert r.status_code == 200 and r.json()["profiles"] == {}

    async with SessionLocal() as s:
        events = (
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.target_id == uuid.UUID(env_id))
                    .where(AuditEvent.action.like("cloud_integration.%"))
                    .order_by(AuditEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [(e.action, e.context["profiles"]) for e in events] == [
        ("cloud_integration.created", ["x-prod.admin", "x-tfstates"]),
        ("cloud_integration.updated", []),
    ]


@pytest.mark.parametrize(
    "profiles",
    [
        {"bad name": PROFILES["x-tfstates"]},  # a space: not an INI section header
        {"x]\n[default": PROFILES["x-tfstates"]},  # section injection
        {"-x": PROFILES["x-tfstates"]},  # must start alphanumeric
        {"x" * 65: PROFILES["x-tfstates"]},  # too long
        {"default": PROFILES["x-tfstates"]},  # reserved: the [default] section
        {"x": {"plan_role_arn": PLAN_ROLE}},  # both roles required
        {"x": {**PROFILES["x-tfstates"], "apply_role_arn": "arn:aws:iam::123:role/x"}},
        {"x": {**PROFILES["x-tfstates"], "plan_role_arn": "arn:aws:iam::123456789012:user/x"}},
        {"x": {**PROFILES["x-tfstates"], "plan_role_arn": f"{PLAN_ROLE}\nregion = x"}},
        {"x": {**PROFILES["x-tfstates"], "region": "eu-west-1\n"}},
        {"x": {**PROFILES["x-tfstates"], "region": "Europe"}},
        {"x": {**PROFILES["x-tfstates"], "profile": "y"}},  # unknown field
        {f"p{i}": PROFILES["x-tfstates"] for i in range(51)},  # over the bound
    ],
)
async def test_invalid_profiles_are_refused(client: httpx.AsyncClient, profiles: dict) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, f"profiles-bad-{uuid.uuid4().hex[:6]}")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    r = await _put(client, admin, env_id, profiles=profiles)
    assert r.status_code == 422, r.text
    missing = await client.get(f"/api/v1/environments/{env_id}/cloud-integration", headers=admin)
    assert missing.status_code == 404  # nothing written


async def test_fifty_profiles_are_accepted(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "profiles-max")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    r = await _put(
        client, admin, env_id, profiles={f"p{i}": PROFILES["x-tfstates"] for i in range(50)}
    )
    assert r.status_code == 200, r.text


async def test_claim_hands_each_profile_the_role_of_the_credential_phase(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-profiles")
    stack = await make_stack(client, admin, "profiles-claim")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    assert (await _put(client, admin, env_id, profiles=PROFILES)).status_code == 200

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    plan = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert plan["cred_phase"] == "plan"
    assert plan["cloud_credentials"]["role_arn"] == PLAN_ROLE
    assert plan["cloud_credentials"]["profiles"] == {
        "x-prod.admin": {"role_arn": ADMIN_PLAN, "region": "us-gov-west-1"},
        "x-tfstates": {"role_arn": STATE_PLAN, "region": None},
    }

    for event in (
        {"event": "phase_started", "phase": "planning"},
        {"event": "phase_finished", "result": {"has_changes": True, "summary": {"add": 1}}},
    ):
        await client.post(f"/worker/v1/jobs/{run['id']}/events", headers=wh, json=event)
    await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    apply = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert apply["cred_phase"] == "apply"
    assert apply["cloud_credentials"]["role_arn"] == APPLY_ROLE
    assert apply["cloud_credentials"]["profiles"] == {
        "x-prod.admin": {"role_arn": ADMIN_APPLY, "region": "us-gov-west-1"},
        "x-tfstates": {"role_arn": STATE_APPLY, "region": None},
    }


async def test_claim_without_profiles_hands_an_empty_map(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-profiles-none")
    stack = await make_stack(client, admin, "profiles-none")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    assert (await _put(client, admin, env_id)).status_code == 200
    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["cloud_credentials"]["profiles"] == {}
