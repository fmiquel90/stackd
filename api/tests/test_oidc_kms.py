"""The workload OIDC issuer's signing key (SPECS §3.11): held in KMS (signing through kms:Sign,
the private key never in the API), rotated with overlap, retired once its tokens expired."""

from __future__ import annotations

import asyncio
import uuid
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
from app.models.run import Run
from app.oidc.issuer import max_token_ttl_seconds, retire_keys, rotate_key
from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

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


async def test_switching_back_to_a_known_kms_key_reactivates_it(
    client: httpx.AsyncClient, kms_key: tuple[str, Callable[[], None]]
) -> None:
    """kms → local → kms on the same key: the key signs again (same kid), no 409 on the JWKS."""
    _arn, switch = kms_key
    admin = await login(client, "admin")
    await client.get("/oidc/jwks")
    switch()
    try:
        first = jwt.get_unverified_header(await _claim_token(client, admin, "kms-back-1"))["kid"]
        await _back_to_local()  # the KMS key retires, a local key signs
        r = await client.get("/oidc/jwks")
        assert r.status_code == 200
        # Still configured for KMS: the next signature switches back to the same key.
        second = jwt.get_unverified_header(await _claim_token(client, admin, "kms-back-2"))["kid"]
        assert second == first
        assert (await client.get("/oidc/jwks")).status_code == 200
    finally:
        await _back_to_local()


async def test_a_job_whose_token_cannot_be_signed_fails_closed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KMS unusable at claim time: the run fails (reason oidc), the worker gets nothing (§10)."""
    from app.oidc import issuer

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "pool-oidc-fail")
    env_id = await make_env(
        client, admin, await make_stack(client, admin, "oidc-fail"), "dev", "dev"
    )
    await client.put(
        f"/api/v1/environments/{env_id}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": PLAN_ROLE},
    )

    async def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("kms unreachable")

    monkeypatch.setattr(issuer, "_sign", broken)
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 204
    failed = (await client.get(f"/api/v1/runs/{run['id']}", headers=admin)).json()
    assert failed["state"] == "failed" and "oidc_signing" in (failed["error"] or "")


# --- Key provisioning under concurrency, and signing failures at claim (§3.11) ---------------

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _oidc_env(client: httpx.AsyncClient, admin: dict, name: str) -> tuple[dict, str]:
    """A worker of its own and an environment with a cloud integration (its jobs get a token)."""
    wh = await register_worker(client, admin, f"pool-{name}")
    env_id = await make_env(client, admin, await make_stack(client, admin, name), "dev", "dev")
    r = await client.put(
        f"/api/v1/environments/{env_id}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": PLAN_ROLE},
    )
    assert r.status_code == 200, r.text
    return wh, env_id


async def _trigger(client: httpx.AsyncClient, admin: dict, env_id: str) -> str:
    r = await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _run(client: httpx.AsyncClient, admin: dict, run_id: str) -> dict:
    return dict((await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json())


def _client_error(code: str, status: int, message: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Sign",
    )


def _slow_kms_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the provisioning window, so concurrent first uses all find no KMS key yet."""
    from app.oidc import issuer

    real = issuer._new_kms_key

    async def slow(arn: str) -> OidcSigningKey:
        key = await real(arn)
        await asyncio.sleep(0.3)
        return key

    monkeypatch.setattr(issuer, "_new_kms_key", slow)


async def _rotations_to(arn: str) -> int:
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "oidc_key.rotated")
                )
            )
            .scalars()
            .all()
        )
    return sum((e.context or {}).get("kms_key_arn") == arn for e in rows)


async def test_concurrent_claims_provision_the_kms_key_once(
    client: httpx.AsyncClient,
    kms_key: tuple[str, Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four claims, each the KMS key's first use: one provisions it, all four sign with it."""
    arn, switch = kms_key
    admin = await login(client, "admin")
    await client.get("/oidc/jwks")  # a local key signs
    envs = [await _oidc_env(client, admin, f"kms-race-{i}") for i in range(4)]
    runs = [await _trigger(client, admin, env_id) for _, env_id in envs]
    _slow_kms_registration(monkeypatch)
    switch()
    try:
        claims = await asyncio.gather(
            *(client.post("/worker/v1/jobs/claim", headers=wh) for wh, _ in envs)
        )
        assert [c.status_code for c in claims] == [200] * 4, [c.text for c in claims]
        kids = {
            jwt.get_unverified_header(c.json()["cloud_credentials"]["oidc_token"])["kid"]
            for c in claims
        }
        assert len(kids) == 1
        assert {c.json()["job_id"] for c in claims} == set(runs)
        for run_id in runs:  # each claim kept its transition
            got = await _run(client, admin, run_id)
            assert got["state"] == "preparing" and got["error"] is None
        assert await _rotations_to(arn) == 1
    finally:
        await _back_to_local()


async def test_concurrent_jwks_fetches_provision_the_kms_key_once(
    client: httpx.AsyncClient,
    kms_key: tuple[str, Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unauthenticated JWKS (what AWS fetches) never answers 409 to a concurrent first use."""
    arn, switch = kms_key
    await client.get("/oidc/jwks")
    _slow_kms_registration(monkeypatch)
    switch()
    try:
        answers = await asyncio.gather(*(client.get("/oidc/jwks") for _ in range(6)))
        assert [r.status_code for r in answers] == [200] * 6, [r.text for r in answers]
        async with SessionLocal() as session:
            active = (
                await session.execute(
                    select(OidcSigningKey).where(OidcSigningKey.status == OidcKeyStatus.active)
                )
            ).scalar_one()
        assert active.kms_key_arn == arn
        assert all(active.kid in {k["kid"] for k in r.json()["keys"]} for r in answers)
        assert await _rotations_to(arn) == 1
    finally:
        await _back_to_local()


async def test_a_claim_that_provisions_the_key_keeps_its_transition(
    client: httpx.AsyncClient,
) -> None:
    """No active key: the claim creates one (in a session of its own) and still hands out the job,
    its run claimed — key management neither commits nor rolls back the claim."""
    admin = await login(client, "admin")
    wh, env_id = await _oidc_env(client, admin, "provision-at-claim")
    run_id = await _trigger(client, admin, env_id)
    async with SessionLocal() as session:
        active = (
            await session.execute(
                select(OidcSigningKey).where(OidcSigningKey.status == OidcKeyStatus.active)
            )
        ).scalar_one_or_none()
        if active is not None:  # no active key at all: the next signature provisions one
            active.status = OidcKeyStatus.retiring
            active.retiring_at = datetime.now(UTC)
            await session.commit()
        previous = active.kid if active is not None else None

    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 200, r.text
    kid = jwt.get_unverified_header(r.json()["cloud_credentials"]["oidc_token"])["kid"]
    assert kid != previous
    got = await _run(client, admin, run_id)
    assert got["state"] == "preparing" and got["worker_id"] is not None
    await _verify(client, r.json()["cloud_credentials"]["oidc_token"])


async def test_provisioning_that_loses_a_race_uses_the_winners_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unique violation while provisioning (a replica that does not take the lock made the key
    first): the winner's key signs, and the caller's transaction is left alone."""
    from sqlalchemy.exc import IntegrityError

    from app.oidc import issuer

    async with SessionLocal() as session:
        active = (
            await session.execute(
                select(OidcSigningKey).where(OidcSigningKey.status == OidcKeyStatus.active)
            )
        ).scalar_one()
        active.status = OidcKeyStatus.retiring
        active.retiring_at = datetime.now(UTC)
        await session.commit()

    winner: dict[str, str] = {}

    async def lose_the_race(own: object, **kwargs: object) -> OidcSigningKey:
        async with SessionLocal() as other:  # the competitor commits its key first…
            key = issuer._new_local_key()
            other.add(key)
            await other.commit()
            winner["kid"] = key.kid
        raise IntegrityError("INSERT INTO oidc_signing_keys", {}, Exception("23505"))  # …we lose

    monkeypatch.setattr(issuer, "rotate_key", lose_the_race)
    async with SessionLocal() as caller:
        audit = AuditEvent(action="test.caller_write", actor_kind="system", target_kind="test")
        caller.add(audit)
        await caller.flush()  # the caller's transaction has work of its own in flight
        key = await issuer.ensure_active_key(caller)
        assert key.kid == winner["kid"]
        assert audit in caller  # not rolled back underneath the caller
        await caller.rollback()


async def test_a_transient_signing_failure_leaves_the_run_to_retry(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KMS throttling at an approved apply's claim: no failed run, no lost approval — the claim
    is undone (503, retryable) and the next poll hands the apply out."""
    from app.oidc import issuer

    admin = await login(client, "admin")
    wh, env_id = await _oidc_env(client, admin, "oidc-throttled")
    run_id = await _trigger(client, admin, env_id)
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run_id, "phase_started", phase="planning")
    await event(client, wh, run_id, "phase_finished", result=CHANGES)
    confirmed = await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
    assert confirmed.status_code == 200, confirmed.text

    real = issuer._sign

    async def throttled(*args: object, **kwargs: object) -> str:
        raise _client_error("ThrottlingException", 400, "Rate exceeded")

    monkeypatch.setattr(issuer, "_sign", throttled)
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "kms_throttled" and r.json()["retryable"] is True
    got = await _run(client, admin, run_id)
    assert got["state"] == "confirmed" and got["error"] is None

    monkeypatch.setattr(issuer, "_sign", real)
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 200, r.text
    assert r.json()["phase"] == "apply" and r.json()["job_id"] == run_id
    assert (await _run(client, admin, run_id))["state"] == "applying"


async def test_no_db_connection_for_provisioning_is_transient(
    client: httpx.AsyncClient,
    kms_key: tuple[str, Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provisioning needs a second pool connection while the claim holds one: a pool timeout is
    retried, never a failed run."""
    import sqlalchemy.exc

    import app.db

    _arn, switch = kms_key
    admin = await login(client, "admin")
    await client.get("/oidc/jwks")
    wh, env_id = await _oidc_env(client, admin, "oidc-pool-timeout")
    run_id = await _trigger(client, admin, env_id)
    switch()  # the next signature has to provision the KMS key
    try:

        def exhausted() -> object:
            raise sqlalchemy.exc.TimeoutError("QueuePool limit of size 5 overflow 10 reached")

        monkeypatch.setattr(app.db, "SessionLocal", exhausted)
        r = await client.post("/worker/v1/jobs/claim", headers=wh)
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "db_unavailable"
        got = await _run(client, admin, run_id)
        assert got["state"] == "queued" and got["worker_id"] is None
    finally:
        monkeypatch.undo()
        await _back_to_local()


async def test_a_permanent_signing_failure_stores_a_generic_code_only(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KMS access denied: the run fails closed, its error and the audit carry a stable code —
    never the AWS message (assumed-role ARN, account id, key ARN)."""
    from app.oidc import issuer

    admin = await login(client, "admin")
    wh, env_id = await _oidc_env(client, admin, "oidc-denied")
    leak = (
        "User: arn:aws:sts::123456789012:assumed-role/stackd-api/i-0abc is not authorized to "
        "perform: kms:Sign on resource: arn:aws:kms:eu-west-1:123456789012:key/1234"
    )

    async def denied(*args: object, **kwargs: object) -> str:
        raise _client_error("AccessDeniedException", 400, leak)

    monkeypatch.setattr(issuer, "_sign", denied)
    run_id = await _trigger(client, admin, env_id)
    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 204
    got = await _run(client, admin, run_id)
    assert got["state"] == "failed" and got["error"] == "oidc_signing:kms_access_denied"
    async with SessionLocal() as session:
        failed = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "run.apply_failed",
                    AuditEvent.target_id == uuid.UUID(run_id),
                )
            )
        ).scalar_one()
    assert failed.context["reason"] == "oidc"
    assert failed.context["detail"] == "oidc_signing:kms_access_denied"
    assert "123456789012" not in str(got) and "123456789012" not in str(failed.context)


async def test_a_failed_signature_discards_what_the_payload_staged(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job never handed out consumed no mock: the failed claim keeps neither the run's
    used_mocks / inputs / provenance nor a `dependency.mock_consumed` audit."""
    from app.oidc import issuer

    admin = await login(client, "admin")
    up = await make_env(client, admin, await make_stack(client, admin, "staged-up"), "dev", "dev")
    wh, env_id = await _oidc_env(client, admin, "staged-down")
    refs = [{"output_name": "net", "input_name": "net", "mock_value": "mock-net"}]
    dep = await client.post(
        f"/api/v1/environments/{env_id}/dependencies",
        headers=admin,
        json={"upstream_env_id": up, "references": refs},
    )
    assert dep.status_code == 201, dep.text

    async def denied(*args: object, **kwargs: object) -> str:
        raise _client_error("DisabledException", 400, "arn:aws:kms:eu-west-1:1:key/x is disabled")

    monkeypatch.setattr(issuer, "_sign", denied)
    run_id = await _trigger(client, admin, env_id)
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 204
    got = await _run(client, admin, run_id)
    assert got["state"] == "failed" and got["error"] == "oidc_signing:kms_key_disabled"
    assert got["used_mocks"] is False and got["variable_provenance"] is None
    async with SessionLocal() as session:
        run = await session.get(Run, uuid.UUID(run_id))
        assert run is not None and run.resolved_inputs is None
        consumed = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "dependency.mock_consumed",
                        AuditEvent.target_id == uuid.UUID(run_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert consumed == []


def test_signing_failures_are_classified() -> None:
    import botocore.exceptions
    import sqlalchemy.exc

    from app.errors import ProblemException
    from app.oidc.issuer import classify_signing_failure

    cases: list[tuple[BaseException, tuple[bool, str]]] = [
        (_client_error("ThrottlingException", 400, "x"), (True, "kms_throttled")),
        (_client_error("KMSInternalException", 500, "x"), (True, "kms_unavailable")),
        (_client_error("SomethingNew", 503, "x"), (True, "kms_unavailable")),
        (botocore.exceptions.EndpointConnectionError(endpoint_url="x"), (True, "kms_unreachable")),
        (botocore.exceptions.ReadTimeoutError(endpoint_url="x"), (True, "kms_unreachable")),
        (sqlalchemy.exc.TimeoutError("pool"), (True, "db_unavailable")),
        (
            ProblemException(503, "No signing key", code="oidc_no_active_key"),
            (True, "oidc_no_active_key"),
        ),
        (_client_error("AccessDeniedException", 400, "x"), (False, "kms_access_denied")),
        (_client_error("DisabledException", 400, "x"), (False, "kms_key_disabled")),
        (_client_error("KMSInvalidStateException", 400, "x"), (False, "kms_key_disabled")),
        (_client_error("NotFoundException", 400, "x"), (False, "kms_key_not_found")),
        (_client_error("InvalidKeyUsageException", 400, "x"), (False, "kms_rejected")),
        (botocore.exceptions.NoCredentialsError(), (False, "kms_no_credentials")),
        (
            ProblemException(422, "KMS key unusable", code="kms_key_unusable"),
            (False, "kms_key_unusable"),
        ),
        (RuntimeError("anything else"), (False, "signing_failed")),
    ]
    for exc, expected in cases:
        assert classify_signing_failure(exc) == expected, exc


async def test_reactivating_a_known_key_takes_the_requested_arn_and_says_so(
    client: httpx.AsyncClient, kms_key: tuple[str, Callable[[], None]]
) -> None:
    """Switching back to a known (retired) KMS key: it signs through the ARN asked for now (a
    replica of the same key material has another ARN), and the audit says it was reactivated."""
    arn, _switch = kms_key
    try:
        async with SessionLocal() as session:
            key = await rotate_key(session, kms_key_arn=arn)
            await session.commit()
            kid = key.kid
        await _back_to_local()
        stale = arn.replace("eu-west-1", "eu-central-1")  # as if registered through a replica
        async with SessionLocal() as session:
            known = (
                await session.execute(select(OidcSigningKey).where(OidcSigningKey.kid == kid))
            ).scalar_one()
            known.kms_key_arn = stale
            known.status = OidcKeyStatus.retired
            known.retired_at = datetime.now(UTC)
            await session.commit()

        async with SessionLocal() as session:
            back = await rotate_key(session, kms_key_arn=arn)
            await session.commit()
            assert back.kid == kid and back.kms_key_arn == arn
            assert back.status == OidcKeyStatus.active and back.retired_at is None
            audit = (
                (
                    await session.execute(
                        select(AuditEvent)
                        .where(AuditEvent.action == "oidc_key.rotated")
                        .order_by(AuditEvent.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert audit is not None and audit.context is not None
        assert audit.context["reactivated"] is True
        assert audit.context["previous_status"] == "retired"
        assert audit.context["replaced_kms_key_arn"] == stale
        assert audit.context["kms_key_arn"] == arn
    finally:
        await _back_to_local()
