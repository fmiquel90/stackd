"""A production API runs jobs on the `docker` runner only (SPECS §7.4), and refuses to start on
dev-grade settings (§13)."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models.audit import AuditEvent

PROD_ENV = {
    "STACKD_ENV": "production",
    "STACKD_DEV_AUTH": "false",
    "STACKD_PUBLIC_URL": "https://stackd.example.com",
    "STACKD_ALLOW_LOCAL_SIGNER": "true",  # the KMS signer has its own tests (test_oidc_kms.py)
}


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    """A switch to production for the rest of the test (called after the dev-login setup)."""

    def switch() -> None:
        for k, v in PROD_ENV.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield switch
    monkeypatch.undo()
    get_settings.cache_clear()


async def _pool(client: httpx.AsyncClient, name: str) -> dict[str, str]:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    admin = {"Authorization": f"Bearer {r.json()['access_token']}"}
    token = (await client.post("/api/v1/worker-pools", headers=admin, json={"name": name})).json()
    return {"Authorization": f"Bearer {token['token']}"}


async def _register(client: httpx.AsyncClient, pool: dict, name: str, runner: str | None):
    body: dict = {"name": name}
    if runner is not None:
        body["runner"] = runner
    return await client.post("/worker/v1/register", headers=pool, json=body)


async def test_production_refuses_a_non_docker_worker(
    client: httpx.AsyncClient, production: Callable[[], None]
) -> None:
    pool = await _pool(client, "prod-runner-pool")
    production()

    for runner in ("local", None):  # None: an agent that predates the field — fail closed
        r = await _register(client, pool, f"agent-{runner}", runner)
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "runner_not_allowed"

    ok = await _register(client, pool, "agent-docker", "docker")
    assert ok.status_code == 200, ok.text

    async with SessionLocal() as session:
        rejected = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "worker.rejected")
                )
            )
            .scalars()
            .all()
        )
    assert {(e.context or {}).get("runner") for e in rejected} >= {"local", None}

    # The agent retries forever: one audit row per worker and hour, not one per attempt.
    for _ in range(3):
        assert (await _register(client, pool, "agent-local", "local")).status_code == 403
    async with SessionLocal() as session:
        again = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "worker.rejected")
                )
            )
            .scalars()
            .all()
        )
    assert sum((e.context or {}).get("name") == "agent-local" for e in again) == 1


async def test_production_refuses_a_claim_from_a_local_worker(
    client: httpx.AsyncClient, production: Callable[[], None]
) -> None:
    """A worker registered while the API was not in production keeps a valid token: the claim
    re-checks the runner."""
    pool = await _pool(client, "prod-claim-pool")
    reg = await _register(client, pool, "agent-before", "local")
    wh = {"Authorization": f"Bearer {reg.json()['worker_token']}"}
    production()

    r = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert r.status_code == 403 and r.json()["code"] == "runner_not_allowed"


async def test_development_accepts_the_local_runner(client: httpx.AsyncClient) -> None:
    pool = await _pool(client, "dev-runner-pool")
    r = await _register(client, pool, "agent-local", "local")
    assert r.status_code == 200, r.text


KEY = base64.b64encode(b"k" * 32).decode()


def test_production_settings_refuse_dev_grade_values() -> None:
    good = {
        "stackd_env": "production",
        "stackd_dev_auth": False,  # the test session's env enables it
        "stackd_encryption_key": KEY,
        "stackd_jwt_secret": "s" * 32,
        "stackd_public_url": "https://stackd.example.com",
        "stackd_oidc_signer": "kms",
        "stackd_oidc_kms_key_arn": "arn:aws:kms:eu-west-1:123456789012:key/abc",
    }
    Settings(**good)  # type: ignore[arg-type]
    for field, bad in (
        ("stackd_encryption_key", ""),
        ("stackd_jwt_secret", "short"),
        ("stackd_public_url", "http://stackd.example.com"),
        ("stackd_oidc_signer", "local"),  # a signing key in the DB: only with an acknowledgement
    ):
        with pytest.raises(ValueError, match="STACKD_ENV=production"):
            Settings(**{**good, field: bad})  # type: ignore[arg-type]
    Settings(**{**good, "stackd_oidc_signer": "local", "stackd_allow_local_signer": True})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="STACKD_OIDC_KMS_KEY_ARN"):
        Settings(stackd_oidc_signer="kms")  # type: ignore[call-arg]
    # Development keeps its defaults.
    Settings(stackd_env="development")  # type: ignore[call-arg]
