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
