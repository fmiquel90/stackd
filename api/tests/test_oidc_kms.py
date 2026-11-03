"""The workload OIDC issuer's signing key (SPECS §3.11): held in KMS (signing through kms:Sign,
the private key never in the API), rotated with overlap, retired once its tokens expired."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import boto3
import httpx
import jwt
import pytest
from moto import mock_aws
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.enums import OidcKeyStatus
from app.models.audit import AuditEvent
from app.models.oidc import OidcSigningKey
from app.oidc.issuer import max_token_ttl_seconds, retire_keys, rotate_key
from tests.conftest_phase2 import login, make_env, make_stack, register_worker

PLAN_ROLE = "arn:aws:iam::123456789012:role/plan"


async def _claim_token(client: httpx.AsyncClient, admin: dict, name: str) -> str:
    wh = await register_worker(client, admin, f"pool-{name}")
    env_id = await make_env(client, admin, await make_stack(client, admin, name), "dev", "dev")
    r = await client.put(
        f"/api/v1/environments/{env_id}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": PLAN_ROLE},
    )
    assert r.status_code == 200, r.text
    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    return (await client.post("/worker/v1/jobs/claim", headers=wh)).json()["cloud_credentials"][
        "oidc_token"
    ]


async def _verify(client: httpx.AsyncClient, token: str) -> dict:
    kid = jwt.get_unverified_header(token)["kid"]
    keys = (await client.get("/oidc/jwks")).json()["keys"]
    jwk = next(k for k in keys if k["kid"] == kid)
    key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    return jwt.decode(token, key, algorithms=["RS256"], audience="sts.amazonaws.com")  # type: ignore[arg-type]


async def _back_to_local() -> None:
    """Leave the shared test DB with a local signing key (other tests sign without moto)."""
    async with SessionLocal() as session:
        await rotate_key(session)
        await session.commit()


@pytest.fixture
def kms_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Callable[[], None]]]:
    """A KMS signing key (moto), and a switch that makes it the configured signer."""
    with mock_aws():
        kms = boto3.client("kms", region_name="eu-west-1")
        arn = kms.create_key(KeySpec="RSA_2048", KeyUsage="SIGN_VERIFY")["KeyMetadata"]["Arn"]

        def switch() -> None:
            monkeypatch.setenv("STACKD_OIDC_SIGNER", "kms")
            monkeypatch.setenv("STACKD_OIDC_KMS_KEY_ARN", arn)
            get_settings.cache_clear()

        yield arn, switch
    monkeypatch.undo()
    get_settings.cache_clear()


async def test_kms_signer_signs_through_kms(
    client: httpx.AsyncClient, kms_key: tuple[str, Callable[[], None]]
) -> None:
    kms_signer, switch = kms_key
    admin = await login(client, "admin")
    await client.get("/oidc/jwks")  # the issuer has a (local) signing key
    switch()
    try:
        # The configured KMS key takes over from the local one: a rotation, audited.
        token = await _claim_token(client, admin, "kms-signed")
        claims = await _verify(client, token)
        assert claims["sub"] == "run:dev:kms-signed:plan"

        async with SessionLocal() as session:
            active = (
                await session.execute(
                    select(OidcSigningKey).where(OidcSigningKey.status == OidcKeyStatus.active)
                )
            ).scalar_one()
            assert active.kms_key_arn == kms_signer and active.private_key_encrypted is None
            assert jwt.get_unverified_header(token)["kid"] == active.kid
            rotated = (
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.action == "oidc_key.rotated")
                    )
                )
                .scalars()
                .all()
            )
            assert any((e.context or {}).get("kms_key_arn") == kms_signer for e in rotated)

        # The previous (local) key is still published: tokens it signed keep verifying.
        listed = (await client.get("/api/v1/oidc/keys", headers=admin)).json()
        assert listed[0]["backend"] == "kms" and listed[0]["status"] == "active"
        assert any(k["status"] == "retiring" and k["backend"] == "local" for k in listed)
        assert "private" not in str(listed)

        # A KMS issuer rotates only to a named KMS key.
        r = await client.post("/api/v1/oidc/keys/rotate", headers=admin, json={})
        assert r.status_code == 422
    finally:
        await _back_to_local()


async def test_kms_key_that_cannot_sign_is_refused(
    client: httpx.AsyncClient, kms_key: tuple[str, Callable[[], None]]
) -> None:
    admin = await login(client, "admin")
    kms = boto3.client("kms", region_name="eu-west-1")
    enc = kms.create_key(KeySpec="SYMMETRIC_DEFAULT", KeyUsage="ENCRYPT_DECRYPT")["KeyMetadata"]
    try:
        r = await client.post(
            "/api/v1/oidc/keys/rotate", headers=admin, json={"kms_key_arn": enc["Arn"]}
        )
        assert r.status_code == 422, r.text
    finally:
        await _back_to_local()


async def test_rotation_keeps_the_old_key_published(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    before = await _claim_token(client, admin, "rotate-before")
    r = await client.post("/api/v1/oidc/keys/rotate", headers=admin, json={})
    assert r.status_code == 200, r.text
    new_kid = r.json()["kid"]
    after = await _claim_token(client, admin, "rotate-after")
    assert jwt.get_unverified_header(after)["kid"] == new_kid
    assert jwt.get_unverified_header(before)["kid"] != new_kid
    await _verify(client, before)  # the retiring key still verifies what it signed
    await _verify(client, after)

    alice = await login(client, "alice")
    assert (
        await client.post("/api/v1/oidc/keys/rotate", headers=alice, json={})
    ).status_code == 403


async def test_retiring_key_leaves_the_jwks_once_its_tokens_expired(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    old_kid = (await client.get("/api/v1/oidc/keys", headers=admin)).json()[0]["kid"]
    assert (
        await client.post("/api/v1/oidc/keys/rotate", headers=admin, json={})
    ).status_code == 200

    async with SessionLocal() as session:
        now = datetime.now(UTC)
        assert await retire_keys(session, now) == 0  # too early: its tokens may still be live
        later = now + timedelta(seconds=max_token_ttl_seconds() + 1)
        assert await retire_keys(session, later) >= 1
        old = (
            await session.execute(select(OidcSigningKey).where(OidcSigningKey.kid == old_kid))
        ).scalar_one()
        assert old.status == OidcKeyStatus.retired and old.retired_at is not None

    kids = {k["kid"] for k in (await client.get("/oidc/jwks")).json()["keys"]}
    assert old_kid not in kids
