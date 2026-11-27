"""Object storage failures (SPECS §11): an S3 error is never read as a missing object (tofu would
plan from an empty state, a worker would report a missing saved plan), S3 I/O holds no run row
lock, and every AWS client is built once, with bounded timeouts."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker
from tests.test_log_retention import _finished_run_with_logs
from tests.test_state_backend import _basic, _claim_apply, _state_doc

PLAN = b"PK\x03\x04 tfplan ... db_password=SUPER-SECRET-VALUE"
CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}
# Throttling, a refused credential, a server error: none of them says the object is missing.
S3_ERRORS = [("SlowDown", 503), ("AccessDenied", 403), ("InternalError", 500)]


@contextmanager
def _s3_fails(operation: str, code: str, status: int) -> Iterator[None]:
    """The process's S3 client answers `operation` once with that error."""
    from app.statebackend.store import _client

    with Stubber(_client()) as stub:
        stub.add_client_error(operation, service_error_code=code, http_status_code=status)
        yield


class _Unreachable:
    def __getattr__(self, _name: str) -> Any:
        def call(*_a: object, **_k: object) -> None:
            raise EndpointConnectionError(endpoint_url="http://s3.invalid")

        return call


async def _rw_env(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, f"{name}-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    token = mint_state_token(environment_id=uuid.UUID(env_id), scope="rw", ttl_seconds=3600)
    return env_id, token


async def _serials(env_id: str) -> list[int]:
    from app.db import SessionLocal

    async with SessionLocal() as s:
        rows = await s.execute(
            text(
                "SELECT serial FROM state_versions WHERE environment_id = :e ORDER BY serial DESC"
            ).bindparams(e=uuid.UUID(env_id))
        )
        return [r[0] for r in rows]


async def _run_row_is_free(run_id: str) -> bool:
    """Whether another transaction can lock the run's row right now (no FOR SHARE/UPDATE held)."""
    from app.db import SessionLocal

    async with SessionLocal() as s:
        try:
            await s.execute(
                text("SELECT id FROM runs WHERE id = :r FOR UPDATE NOWAIT").bindparams(
                    r=uuid.UUID(run_id)
                )
            )
        except DBAPIError:
            return False
        finally:
            await s.rollback()
    return True


# --- state GET ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "status"), S3_ERRORS)
async def test_state_read_error_is_never_an_empty_state(
    client: httpx.AsyncClient, code: str, status: int
) -> None:
    env_id, rw = await _rw_env(client, f"s3get-{code.lower()}")
    url = f"/state/v1/{env_id}"
    with mock_aws():
        assert (
            await client.post(url, headers=_basic(rw), content=_state_doc(3))
        ).status_code == 200
        with _s3_fails("get_object", code, status):
            got = await client.get(url, headers=_basic(rw))
        assert got.status_code == 503, got.text  # retryable — a 404 would read as "no state"
        assert got.json()["title"] == "Object storage unavailable"

        again = await client.get(url, headers=_basic(rw))
        assert again.status_code == 200
        assert again.content == _state_doc(3)
        assert again.headers["content-length"] == str(len(_state_doc(3)))


async def test_state_read_unreachable_s3_is_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.statebackend import store

    env_id, rw = await _rw_env(client, "s3get-unreachable")
    with mock_aws():
        await client.post(f"/state/v1/{env_id}", headers=_basic(rw), content=_state_doc(1))
        monkeypatch.setattr(store, "_client", lambda: _Unreachable())
        got = await client.get(f"/state/v1/{env_id}", headers=_basic(rw))
        assert got.status_code == 503, got.text


async def test_state_version_without_its_object_is_a_5xx(client: httpx.AsyncClient) -> None:
    from app.config import get_settings
    from app.statebackend.store import _client, list_keys

    env_id, rw = await _rw_env(client, "s3get-missing")
    url = f"/state/v1/{env_id}"
    with mock_aws():
        assert (await client.get(url, headers=_basic(rw))).status_code == 404  # no version yet
        await client.post(url, headers=_basic(rw), content=_state_doc(1))
        (key,) = await list_keys(f"states/{env_id}/")
        _client().delete_object(Bucket=get_settings().s3_bucket, Key=key)
        got = await client.get(url, headers=_basic(rw))
        assert got.status_code == 500, got.text
        assert got.json()["title"] == "State object missing"


# --- state POST --------------------------------------------------------------------------------


async def test_state_write_rejects_invalid_and_oversized_bodies(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    env_id, rw = await _rw_env(client, "s3post-bodies")
    url = f"/state/v1/{env_id}"
    with mock_aws():
        for body in (b"{not json", b"[1, 2]", b'{"serial": "x"}'):
            bad = await client.post(url, headers=_basic(rw), content=body)
            assert bad.status_code == 400, (body, bad.text)
        monkeypatch.setattr(get_settings(), "stackd_state_max_bytes", 64)
        big = await client.post(url, headers=_basic(rw), content=_state_doc(1) + b" " * 64)
        assert big.status_code == 413, big.text

        async def chunks():  # no Content-Length: capped while streaming too
            for _ in range(3):
                yield b" " * 32

        streamed = await client.post(url, headers=_basic(rw), content=chunks())
        assert streamed.status_code == 413, streamed.text
        assert await _serials(env_id) == []


async def test_state_write_failing_upload_records_no_version(client: httpx.AsyncClient) -> None:
    env_id, rw = await _rw_env(client, "s3post-fail")
    url = f"/state/v1/{env_id}"
    with mock_aws():
        assert (
            await client.post(url, headers=_basic(rw), content=_state_doc(1))
        ).status_code == 200
        with _s3_fails("put_object", "SlowDown", 503):
            failed = await client.post(url, headers=_basic(rw), content=_state_doc(2))
        assert failed.status_code == 503, failed.text
        assert await _serials(env_id) == [1]


async def test_state_upload_holds_no_run_row_lock(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.statebackend.router as state_router

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "s3post-lock-pool")
    stack = await make_stack(client, admin, "s3post-lock-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id, apply = await _claim_apply(client, admin, wh, env_id)
    token = apply["backend"]["password"]

    free_during_upload: list[bool] = []
    real_put = state_router.put_object

    async def probing_put(key: str, data: bytes) -> None:
        free_during_upload.append(await _run_row_is_free(run_id))
        await real_put(key, data)

    monkeypatch.setattr(state_router, "put_object", probing_put)
    with mock_aws():
        post = await client.post(
            f"/state/v1/{env_id}", headers=_basic(token), content=_state_doc(1)
        )
        assert post.status_code == 200, post.text
    assert free_during_upload == [True]
    assert await _serials(env_id) == [1]


async def test_state_write_refused_after_upload_records_no_version(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run ends while its state uploads: the checks run again under the run's lock, so the
    write is refused (an orphan object, no version)."""
    import app.statebackend.router as state_router

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "s3post-race-pool")
    stack = await make_stack(client, admin, "s3post-race-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run_id, apply = await _claim_apply(client, admin, wh, env_id)
    token = apply["backend"]["password"]
    real_put = state_router.put_object

    async def put_while_the_run_fails(key: str, data: bytes) -> None:
        await event(client, wh, run_id, "job_failed", phase="apply")
        await real_put(key, data)

    monkeypatch.setattr(state_router, "put_object", put_while_the_run_fails)
    with mock_aws():
        post = await client.post(
            f"/state/v1/{env_id}", headers=_basic(token), content=_state_doc(1)
        )
        assert post.status_code == 403, post.text
    assert await _serials(env_id) == []


# --- saved plan --------------------------------------------------------------------------------


def _plan_url(run_id: str) -> str:
    return f"/worker/v1/jobs/{run_id}/artifacts/plan.tfplan"


async def _planning_run(client: httpx.AsyncClient, name: str) -> tuple[dict, dict, str]:
    """A tracked run in its plan job (`planning`). Returns (admin, worker headers, run id)."""
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-{name}")
    stack = await make_stack(client, admin, f"{name}-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
    assert job["job_id"] == run["id"]
    await event(client, wh, run["id"], "phase_started", phase="planning")
    return admin, wh, run["id"]


async def _plan_key(run_id: str) -> str | None:
    from app.db import SessionLocal
    from app.models.run import Run

    async with SessionLocal() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None
        return run.plan_artifact_key


async def test_plan_upload_holds_no_run_row_lock(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.runs.artifacts as artifacts

    _admin, wh, run_id = await _planning_run(client, "sp-nolock")
    free_during_upload: list[bool] = []
    real_put = artifacts.put_object

    async def probing_put(key: str, data: bytes) -> None:
        free_during_upload.append(await _run_row_is_free(run_id))
        await real_put(key, data)

    monkeypatch.setattr(artifacts, "put_object", probing_put)
    with mock_aws():
        put = await client.put(_plan_url(run_id), headers=wh, content=PLAN)
        assert put.status_code == 204, put.text
    assert free_during_upload == [True]


async def test_late_plan_upload_never_overwrites_the_reviewed_plan(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second upload whose plan job ends (→ unconfirmed) while it uploads: refused under the row
    lock, its object dropped — and the reviewed plan's object untouched."""
    import app.runs.artifacts as artifacts
    from app.runs.artifacts import load_plan, wait_for_purges
    from app.statebackend.store import list_keys

    _admin, wh, run_id = await _planning_run(client, "sp-late")
    real_put = artifacts.put_object

    async def put_after_the_plan_job_ends(key: str, data: bytes) -> None:
        await event(client, wh, run_id, "phase_finished", result=CHANGES)
        await real_put(key, data)

    with mock_aws():
        assert (await client.put(_plan_url(run_id), headers=wh, content=PLAN)).status_code == 204
        reviewed = await _plan_key(run_id)
        monkeypatch.setattr(artifacts, "put_object", put_after_the_plan_job_ends)
        late = await client.put(_plan_url(run_id), headers=wh, content=b"PK another plan")
        assert late.status_code == 409, late.text
        await wait_for_purges()
        assert await _plan_key(run_id) == reviewed
        assert await list_keys(f"artifacts/{run_id}/") == [reviewed]
        assert reviewed is not None and await load_plan(reviewed) == PLAN


async def test_retried_plan_upload_replaces_the_previous_object(client: httpx.AsyncClient) -> None:
    from app.runs.artifacts import load_plan, wait_for_purges
    from app.statebackend.store import list_keys

    _admin, wh, run_id = await _planning_run(client, "sp-retry")
    with mock_aws():
        assert (
            await client.put(_plan_url(run_id), headers=wh, content=b"PK first")
        ).status_code == 204
        first = await _plan_key(run_id)
        assert (await client.put(_plan_url(run_id), headers=wh, content=PLAN)).status_code == 204
        second = await _plan_key(run_id)
        assert second is not None and second != first
        await wait_for_purges()
        assert await list_keys(f"artifacts/{run_id}/") == [second]
        assert await load_plan(second) == PLAN


@pytest.mark.parametrize(("code", "status"), S3_ERRORS)
async def test_saved_plan_read_error_is_not_a_missing_plan(
    client: httpx.AsyncClient, code: str, status: int
) -> None:
    from app.config import get_settings
    from app.statebackend.store import _client

    admin, wh, run_id = await _planning_run(client, f"sp-get-{code.lower()}")
    with mock_aws():
        assert (await client.put(_plan_url(run_id), headers=wh, content=PLAN)).status_code == 204
        await event(client, wh, run_id, "phase_finished", result=CHANGES)
        assert (
            await client.post(f"/api/v1/runs/{run_id}/confirm", headers=admin)
        ).status_code == 200
        apply = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()
        assert apply["phase"] == "apply"

        with _s3_fails("get_object", code, status):
            got = await client.get(_plan_url(run_id), headers=wh)
        assert got.status_code == 503, got.text
        assert got.json()["title"] == "Object storage unavailable"
        assert (await client.get(_plan_url(run_id), headers=wh)).content == PLAN

        key = await _plan_key(run_id)
        _client().delete_object(Bucket=get_settings().s3_bucket, Key=key)
        missing = await client.get(_plan_url(run_id), headers=wh)
        assert missing.status_code == 404, missing.text
        assert missing.json()["title"] == "Saved plan missing"


# --- archived logs -----------------------------------------------------------------------------


async def test_archived_logs_read_error_is_not_an_empty_log(client: httpx.AsyncClient) -> None:
    from app.db import SessionLocal
    from app.runs.logs_archive import archive_run_logs, purge_hot_logs

    with mock_aws():
        run_id, admin = await _finished_run_with_logs(client, "s3-logs")
        now = datetime.now(UTC) + timedelta(minutes=2)
        async with SessionLocal() as session:
            await archive_run_logs(session, now)
            await purge_hot_logs(session, now + timedelta(days=8))
        url = f"/api/v1/runs/{run_id}/logs"
        with _s3_fails("list_objects_v2", "SlowDown", 503):
            listed = await client.get(url, headers=admin)
        assert listed.status_code == 503, listed.text
        with _s3_fails("get_object", "InternalError", 500):
            one = await client.get(url, headers=admin, params={"phase": "planning"})
        assert one.status_code == 503, one.text
        assert len((await client.get(url, headers=admin)).json()) == 2


# --- the S3 client itself ----------------------------------------------------------------------


async def test_store_reads_tell_missing_from_unavailable() -> None:
    from app.statebackend.store import StoreUnavailable, get_object, list_keys, put_object

    with mock_aws():
        assert await get_object("nowhere/no-bucket-yet") is None
        assert await list_keys("nowhere/") == []
        await put_object("here/a", b"a")
        assert await get_object("here/missing") is None
        assert await get_object("here/a") == b"a"
        for operation, call in (
            ("get_object", lambda: get_object("here/a")),
            ("list_objects_v2", lambda: list_keys("here/")),
            ("put_object", lambda: put_object("here/b", b"b")),
        ):
            with _s3_fails(operation, "SlowDown", 503), pytest.raises(StoreUnavailable):
                await call()


class _CountingS3:
    def __init__(self, head_error: str | None = None) -> None:
        self.head_error = head_error
        self.calls: list[str] = []

    def head_bucket(self, **_: object) -> None:
        self.calls.append("head_bucket")
        if self.head_error:
            raise ClientError({"Error": {"Code": self.head_error}}, "HeadBucket")

    def create_bucket(self, **_: object) -> None:
        self.calls.append("create_bucket")

    def put_object(self, **_: object) -> None:
        self.calls.append("put_object")


def test_bucket_is_checked_once_and_a_failed_check_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.statebackend import store

    monkeypatch.setattr(store, "_ready_buckets", set())
    ok = _CountingS3()
    monkeypatch.setattr(store, "_client", lambda: ok)
    store._put_sync("k1", b"1")
    store._put_sync("k2", b"2")
    assert ok.calls == ["head_bucket", "put_object", "put_object"]

    monkeypatch.setattr(store, "_ready_buckets", set())
    throttled = _CountingS3(head_error="503")
    monkeypatch.setattr(store, "_client", lambda: throttled)
    with pytest.raises(ClientError):
        store._put_sync("k3", b"3")
    assert throttled.calls == ["head_bucket"]  # no create_bucket on a bucket that may exist
    assert store._ready_buckets == set()  # checked again on the next write


def test_s3_client_is_built_once_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.statebackend import store

    built: list[object] = []

    def slow_build() -> object:
        time.sleep(0.05)
        built.append(object())
        return built[-1]

    monkeypatch.setattr(store, "_s3", None)
    monkeypatch.setattr(store, "_build_client", slow_build)
    got: list[object] = []
    threads = [threading.Thread(target=lambda: got.append(store._client())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
    assert all(c is built[0] for c in got)


def test_aws_clients_have_bounded_timeouts_and_standard_retries() -> None:
    from app.oidc.issuer import _kms_client_for
    from app.statebackend.store import _build_client

    for aws_client in (_build_client(), _kms_client_for("ap-southeast-4")):
        config = aws_client.meta.config
        assert (config.connect_timeout, config.read_timeout) == (5, 30)
        assert config.retries == {"mode": "standard", "total_max_attempts": 3}
