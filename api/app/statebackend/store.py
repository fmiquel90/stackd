from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.aws import client_config
from app.config import get_settings
from app.errors import ProblemException
from app.logging import get_logger

_log = get_logger("stackd.statebackend")

# S3 holds the bytes; the API is the only thing with credentials to it (SPECS §11.1).

# The only answers that mean "this object does not exist" (no bucket: no object either). Anything
# else — throttling, a 5xx, AccessDenied, a timeout — says nothing about the object: reading it as
# "missing" would hand tofu an empty state (a plan that re-creates everything) or a worker a
# "saved plan missing".
_MISSING_KEY = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})
_MISSING_BUCKET = frozenset({"NoSuchBucket", "404", "NotFound"})
_STREAM_CHUNK = 1024 * 1024


class StoreUnavailable(ProblemException):
    """S3 failed to answer: never "no such object". A 503 the caller may retry."""

    def __init__(self, error: str) -> None:
        super().__init__(
            503, "Object storage unavailable", f"S3 did not answer ({error}); retry later."
        )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "unknown"))
    return type(exc).__name__


def _unavailable(op: str, key: str, exc: Exception) -> StoreUnavailable:
    code = _error_code(exc)
    _log.warning(
        "object storage unavailable",
        extra={"event": "s3.unavailable", "op": op, "key": key, "error": code},
    )
    return StoreUnavailable(code)


def _build_client() -> Any:
    s = get_settings()
    if s.s3_access_key or s.s3_secret_key:
        creds = {"aws_access_key_id": s.s3_access_key, "aws_secret_access_key": s.s3_secret_key}
    elif s.is_production:
        # No static keys: the AWS default credential chain (instance profile, ECS task role, IRSA /
        # web identity, environment, shared config) — no long-lived key to store or rotate.
        creds = {}
    else:
        creds = {"aws_access_key_id": "test", "aws_secret_access_key": "test"}  # dev/moto
    # A Session of its own: boto3's default session is not thread-safe to build clients from, and
    # every S3 call runs in a worker thread.
    return boto3.session.Session().client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        region_name=s.aws_region or "us-east-1",
        config=client_config(),
        **creds,
    )


_client_lock = threading.Lock()
_s3: Any = None


def _client() -> Any:
    """One client per process (a client is thread-safe once built; building one per call loads
    the service model and the credential chain each time)."""
    global _s3
    with _client_lock:
        if _s3 is None:
            _s3 = _build_client()
        return _s3


def _ensure_bucket(client: Any, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        # Only a bucket S3 says is missing gets created: a throttled or failed HEAD is an error,
        # not a reason to create (and re-harden) a bucket that exists.
        if _error_code(exc) not in _MISSING_BUCKET:
            raise
        client.create_bucket(Bucket=bucket)
        _harden_bucket(client, bucket)


_bucket_lock = threading.Lock()
_ready_buckets: set[str] = set()


def _ensure_bucket_once(client: Any, bucket: str) -> None:
    with _bucket_lock:
        if bucket not in _ready_buckets:
            _ensure_bucket(client, bucket)
            _ready_buckets.add(bucket)


def _harden_bucket(client: Any, bucket: str) -> None:
    """A bucket the API creates gets versioning ON and default SSE-S3 encryption (SPECS §11.3).
    An S3 implementation without these calls (Garage, in dev) keeps the bucket as is, with a
    warning — never a failed write. A bucket provisioned beforehand is left alone."""
    steps = {
        "versioning": lambda: client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        ),
        "encryption": lambda: client.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        ),
    }
    for name, step in steps.items():
        try:
            step()
        except ClientError as exc:
            _log.warning(
                "could not enable bucket %s",
                name,
                extra={
                    "event": "s3.bucket_setting_unsupported",
                    "bucket": bucket,
                    "setting": name,
                    "error": _error_code(exc),
                },
            )


def _put_sync(key: str, data: bytes) -> None:
    bucket = get_settings().s3_bucket
    client = _client()
    _ensure_bucket_once(client, bucket)
    try:
        client.put_object(Bucket=bucket, Key=key, Body=data)
    except ClientError as exc:
        # The bucket went away since it was checked (an S3 reset): check it again, once.
        if _error_code(exc) != "NoSuchBucket":
            raise
        with _bucket_lock:
            _ready_buckets.discard(bucket)
        _ensure_bucket_once(client, bucket)
        client.put_object(Bucket=bucket, Key=key, Body=data)


def _open_sync(key: str) -> dict[str, Any] | None:
    try:
        return dict(_client().get_object(Bucket=get_settings().s3_bucket, Key=key))
    except ClientError as exc:
        if _error_code(exc) in _MISSING_KEY:
            return None
        raise


def _get_sync(key: str) -> bytes | None:
    resp = _open_sync(key)
    if resp is None:
        return None
    body = resp["Body"]
    try:
        data: bytes = body.read()
        return data
    finally:
        body.close()


def _delete_sync(key: str) -> None:
    # Idempotent in S3: deleting a missing key succeeds.
    _client().delete_object(Bucket=get_settings().s3_bucket, Key=key)


def _list_sync(prefix: str) -> list[str]:
    s = get_settings()
    keys: list[str] = []
    pages = _client().get_paginator("list_objects_v2").paginate(Bucket=s.s3_bucket, Prefix=prefix)
    try:
        for page in pages:
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
    except ClientError as exc:
        if _error_code(exc) != "NoSuchBucket":
            raise
        return []
    return keys


async def list_keys(prefix: str) -> list[str]:
    try:
        return await asyncio.to_thread(_list_sync, prefix)
    except (ClientError, BotoCoreError) as exc:
        raise _unavailable("list", prefix, exc) from exc


async def put_object(key: str, data: bytes) -> None:
    try:
        await asyncio.to_thread(_put_sync, key, data)
    except (ClientError, BotoCoreError) as exc:
        raise _unavailable("put", key, exc) from exc


async def get_object(key: str) -> bytes | None:
    """The object's bytes; None only when S3 says the key does not exist. Any other failure raises
    StoreUnavailable — never read as a missing object."""
    try:
        return await asyncio.to_thread(_get_sync, key)
    except (ClientError, BotoCoreError) as exc:
        raise _unavailable("get", key, exc) from exc


async def open_object(key: str) -> tuple[AsyncIterator[bytes], int] | None:
    """The object as a byte stream and its size, None when the key does not exist (StoreUnavailable
    like get_object). A failure once streaming has started ends the stream short of that size."""
    try:
        resp = await asyncio.to_thread(_open_sync, key)
    except (ClientError, BotoCoreError) as exc:
        raise _unavailable("get", key, exc) from exc
    if resp is None:
        return None
    body = resp["Body"]

    async def chunks() -> AsyncIterator[bytes]:
        try:
            while chunk := await asyncio.to_thread(body.read, _STREAM_CHUNK):
                yield chunk
        finally:
            body.close()

    return chunks(), int(resp["ContentLength"])


async def delete_object(key: str) -> None:
    await asyncio.to_thread(_delete_sync, key)


def state_key(environment_id: str, version_id: str) -> str:
    return f"states/{environment_id}/{version_id}.tfstate"
