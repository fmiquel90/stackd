"""A proposed run gets none of the environment's secrets (SPECS §13): its code is a pull request's,
and repo code runs inside tofu (`data "external"`, provisioners). No sensitive variable of either
kind, no external secret reference (not even fetched), nothing of them in `mask_values` or the
provenance snapshot. It keeps what a plan needs: the read-only plan role's OIDC credentials
(§10.3), its `ro` state token, the repo token to clone. A tracked run, and a drift run (a proposed
run of the env's last applied commit, §19), still get every secret."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.secret_sources import providers
from tests.conftest_phase2 import login, make_env, make_stack, register_worker

PLAN_ROLE = "arn:aws:iam::123456789012:role/stackd-plan"
APPLY_ROLE = "arn:aws:iam::123456789012:role/stackd-apply"
REPO_TOKEN = "ghp_proposedrepotoken"

# name → (kind, sensitive, hcl, value); `dbpass` is an external secret reference, added apart.
VARIABLES = {
    "API_TOKEN": ("environment", True, False, "env-secret-value"),
    "REGION": ("environment", False, False, "eu-west-1"),
    "db_password": ("terraform", True, False, "tf-secret-value"),
    "db_conf": ("terraform", True, True, '{ pw = "hcl-secret-value" }'),
    "size": ("terraform", False, False, "small"),
}
REFERENCE_VALUE = "reference-secret-value"
SECRETS = ["env-secret-value", "tf-secret-value", "hcl-secret-value", REFERENCE_VALUE]


async def _env_with_secrets(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[dict[str, str], dict[str, str], str, list[str]]:
    """An env holding a secret of every kind, a cloud integration and a repo token. Returns the
    admin and worker headers, the env id and the list of references the provider was asked for."""
    from app.crypto import encrypt
    from app.db import SessionLocal
    from app.enums import RepoAuthKind
    from app.models.stack import Stack

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-{name}")
    stack_id = await make_stack(client, admin, f"{name}-stack")
    env_id = await make_env(client, admin, stack_id, "dev", "dev")
    async with SessionLocal() as s:
        stack = await s.get(Stack, uuid.UUID(stack_id))
        assert stack is not None
        stack.repo_auth_kind = RepoAuthKind.token
        stack.repo_secret_encrypted = encrypt(REPO_TOKEN)
        await s.commit()

    r = await client.put(
        f"/api/v1/environments/{env_id}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": APPLY_ROLE, "region": "eu-west-1"},
    )
    assert r.status_code == 200, r.text

    variables = f"/api/v1/environments/{env_id}/variables"
    for var, (kind, sensitive, hcl, value) in VARIABLES.items():
        body = {"kind": kind, "name": var, "value": value, "sensitive": sensitive, "hcl": hcl}
        r = await client.post(variables, headers=admin, json=body)
        assert r.status_code == 201, r.text

    space_id = (await client.get(f"/api/v1/stacks/{stack_id}", headers=admin)).json()["space_id"]
    source = await client.post(
        f"/api/v1/spaces/{space_id}/secret-sources",
        headers=admin,
        json={"name": f"{name}-source", "provider": "proton_pass", "bootstrap_secret": "pat_x"},
    )
    assert source.status_code == 201, source.text
    ref = {
        "kind": "terraform",
        "name": "dbpass",
        "secret_source_id": source.json()["id"],
        "secret_ref": "pass://vault/dbpass/value",
    }
    assert (await client.post(variables, headers=admin, json=ref)).status_code == 201

    fetched: list[str] = []

    async def fetch_secret(_source: object, secret_ref: str) -> str:
        fetched.append(secret_ref)
        return REFERENCE_VALUE

    monkeypatch.setattr(providers, "fetch_secret", fetch_secret)
    return admin, wh, env_id, fetched


async def _claim(client: httpx.AsyncClient, wh: dict[str, str]) -> dict:
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    payload: dict = claim.json()
    return payload


async def test_a_proposed_run_gets_no_secret(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, wh, env_id, fetched = await _env_with_secrets(client, monkeypatch, "proposed-secrets")
    run = await client.post(
        f"/api/v1/environments/{env_id}/runs", headers=admin, json={"type": "proposed"}
    )
    assert run.status_code == 201, run.text
    payload = await _claim(client, wh)

    blob = json.dumps(payload)
    for secret in SECRETS:
        assert secret not in blob, secret  # in no field: env, tfvars, mask_values, …
    assert payload["sensitive_env"] == {}
    assert payload["env"] == {"REGION": "eu-west-1"}
    assert payload["tfvars_json"] == {"size": "small"}
    assert payload["hcl_tfvars"] == {}
    assert fetched == []  # an external reference is not even fetched
    # What a plan needs stays: the plan role (read-only, §10.3), the `ro` state, the clone token.
    assert payload["cloud_credentials"]["role_arn"] == PLAN_ROLE
    assert payload["repo_credentials"]["token"] == REPO_TOKEN
    provenance = (await client.get(f"/api/v1/runs/{run.json()['id']}", headers=admin)).json()[
        "variable_provenance"
    ]
    assert provenance == {"REGION": "env", "TF_VAR_size": "env"}


async def test_a_tracked_run_still_gets_its_secrets(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, wh, env_id, fetched = await _env_with_secrets(client, monkeypatch, "tracked-secrets")
    run = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    assert run.status_code == 201, run.text
    payload = await _claim(client, wh)

    assert payload["sensitive_env"] == {"API_TOKEN": "env-secret-value"}
    assert payload["tfvars_json"] == {
        "db_password": "tf-secret-value",
        "dbpass": REFERENCE_VALUE,
        "size": "small",
    }
    assert payload["hcl_tfvars"] == {"db_conf": '{ pw = "hcl-secret-value" }'}
    assert fetched == ["pass://vault/dbpass/value"]
    for secret in ("env-secret-value", "tf-secret-value", REFERENCE_VALUE):
        assert secret in payload["mask_values"]


async def test_a_drift_run_keeps_the_secrets(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A drift run is a proposed run of the env's last applied commit (§19.2) — reviewed code that
    # already ran with these secrets; its refresh needs them as much as an apply did.
    from app.db import SessionLocal
    from app.enums import RunType, TriggeredBy
    from app.models.environment import Environment
    from app.runs.service import trigger_run

    _admin, wh, env_id, _fetched = await _env_with_secrets(client, monkeypatch, "drift-secrets")
    async with SessionLocal() as s:
        env = await s.get(Environment, uuid.UUID(env_id))
        assert env is not None
        await trigger_run(
            s,
            env,
            run_type=RunType.proposed,
            triggered_by=TriggeredBy.schedule,
            commit_sha="a" * 40,
            is_drift=True,
        )
    payload = await _claim(client, wh)

    assert payload["refresh_only"] is True
    assert payload["sensitive_env"] == {"API_TOKEN": "env-secret-value"}
    assert payload["tfvars_json"]["dbpass"] == REFERENCE_VALUE
