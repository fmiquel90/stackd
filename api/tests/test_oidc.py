from __future__ import annotations

import httpx
import jwt
from jwt import PyJWKClient
from moto import mock_aws

from tests.conftest_phase2 import login, make_env, make_stack, register_worker

PLAN_ROLE = "arn:aws:iam::123456789012:role/stackd-prod-plan"
APPLY_ROLE = "arn:aws:iam::123456789012:role/stackd-prod-apply"


async def _put_integration(client, admin, env_id) -> None:
    r = await client.put(
        f"/api/v1/environments/{env_id}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": APPLY_ROLE, "region": "eu-west-1"},
    )
    assert r.status_code == 200, r.text


async def test_issuer_metadata_and_jwks(client: httpx.AsyncClient) -> None:
    cfg = (await client.get("/.well-known/openid-configuration")).json()
    assert cfg["jwks_uri"].endswith("/oidc/jwks")
    assert cfg["id_token_signing_alg_values_supported"] == ["RS256"]
    keys = (await client.get("/oidc/jwks")).json()["keys"]
    assert keys and keys[0]["kty"] == "RSA" and keys[0]["alg"] == "RS256"


async def test_claim_payload_signs_plan_token(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-oidc")
    stack = await make_stack(client, admin, "oidc-core-network")
    env_id = await make_env(client, admin, stack, "prod", "prod")
    await _put_integration(client, admin, env_id)

    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    creds = payload["cloud_credentials"]
    assert creds["role_arn"] == PLAN_ROLE  # plan job → plan role (§10.3)

    # Token verifies against the published JWKS, with the §10.2 claims.
    token = creds["oidc_token"]
    base = "http://localhost:8000"
    signing_key = PyJWKClient(f"{base}/oidc/jwks")  # not fetched; decode with the jwk directly
    header = jwt.get_unverified_header(token)
    jwks = (await client.get("/oidc/jwks")).json()["keys"]
    jwk = next(k for k in jwks if k["kid"] == header["kid"])
    key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    claims = jwt.decode(token, key, algorithms=["RS256"], audience="sts.amazonaws.com")
    assert claims["sub"] == "run:prod:oidc-core-network:plan"
    assert claims["tier"] == "prod"
    assert claims["phase"] == "plan"
    _ = signing_key


async def test_apply_uses_apply_role(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-oidc-apply")
    stack = await make_stack(client, admin, "oidc-apply-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    await _put_integration(client, admin, env_id)

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    await client.post("/worker/v1/jobs/claim", headers=wh)
    await client.post(
        f"/worker/v1/jobs/{run['id']}/events",
        headers=wh,
        json={"event": "phase_started", "phase": "planning"},
    )
    await client.post(
        f"/worker/v1/jobs/{run['id']}/events",
        headers=wh,
        json={"event": "phase_finished", "result": {"has_changes": True, "summary": {"add": 1}}},
    )
    await client.post(f"/api/v1/runs/{run['id']}/confirm", headers=admin)
    apply_payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert apply_payload["phase"] == "apply"
    assert apply_payload["cloud_credentials"]["role_arn"] == APPLY_ROLE


async def test_assume_role_against_moto(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "oidc-test-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    await _put_integration(client, admin, env_id)
    with mock_aws():
        resp = await client.post(
            f"/api/v1/environments/{env_id}/cloud-integration/test", headers=admin
        )
        assert resp.status_code == 200
        assert "assumed_role" in resp.json()


async def _env_var(client, admin, env_id, name, value, *, sensitive=False) -> None:
    r = await client.post(
        f"/api/v1/environments/{env_id}/variables",
        headers=admin,
        json={"kind": "environment", "name": name, "value": value, "sensitive": sensitive},
    )
    assert r.status_code == 201, r.text


async def test_oidc_shadows_static_aws_credentials(client: httpx.AsyncClient) -> None:
    """§10.5: with a cloud integration, a static AWS credential never reaches the job — the AWS
    SDK would pick it before the web identity token — and the integration reports it by name."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-oidc-shadow")
    stack = await make_stack(client, admin, "oidc-shadow-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    await _env_var(client, admin, env_id, "AWS_ACCESS_KEY_ID", "AKIASTATICKEY0000000")
    await _env_var(client, admin, env_id, "AWS_SECRET_ACCESS_KEY", "static-secret", sensitive=True)
    await _env_var(client, admin, env_id, "AWS_REGION", "eu-west-1")
    await _put_integration(client, admin, env_id)

    got = (
        await client.get(f"/api/v1/environments/{env_id}/cloud-integration", headers=admin)
    ).json()
    assert got["warnings"] == [
        {
            "code": "static_aws_credentials_shadowed",
            "variables": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
            "detail": got["warnings"][0]["detail"],
        }
    ]
    assert "static-secret" not in str(got)

    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    resp = await client.post("/worker/v1/jobs/claim", headers=wh)
    payload = resp.json()
    assert "AKIASTATICKEY0000000" not in resp.text and "static-secret" not in resp.text
    assert "AWS_ACCESS_KEY_ID" not in payload["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in payload["sensitive_env"]
    assert payload["env"]["AWS_REGION"] == "eu-west-1"  # a setting, not a credential: kept
    assert payload["cloud_credentials"]["role_arn"] == PLAN_ROLE

    detail = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert detail["variable_provenance"]["AWS_ACCESS_KEY_ID"] == "shadowed:env"
    assert detail["variable_provenance"]["AWS_SECRET_ACCESS_KEY"] == "shadowed:env"


async def test_static_aws_credentials_kept_without_integration(client: httpx.AsyncClient) -> None:
    """No integration → the static fallback (§10.4) is the env's credential: passed as before."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-oidc-static")
    stack = await make_stack(client, admin, "oidc-static-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    await _env_var(client, admin, env_id, "AWS_ACCESS_KEY_ID", "AKIASTATICKEY0000001")

    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    payload = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert payload["env"]["AWS_ACCESS_KEY_ID"] == "AKIASTATICKEY0000001"
    assert payload["cloud_credentials"] is None
