from __future__ import annotations

import asyncio

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings

# S3 holds the bytes; the API is the only thing with credentials to it (SPECS §11.1).


def _client_kwargs() -> dict[str, str | None]:
    """boto3 S3 client settings. Static keys only when both are configured (the dev Garage, an
    on-prem S3): otherwise no credentials are passed and boto3 resolves its default chain — the
    ECS task role, an instance profile, `AWS_*` env vars — instead of signing with a dummy key."""
    s = get_settings()
    kwargs: dict[str, str | None] = {
        "endpoint_url": s.s3_endpoint_url,
        "region_name": s.aws_region or "us-east-1",
    }
    if s.s3_access_key and s.s3_secret_key:
        kwargs["aws_access_key_id"] = s.s3_access_key
        kwargs["aws_secret_access_key"] = s.s3_secret_key
    elif s.s3_access_key or s.s3_secret_key:
        # Half a key pair is a misconfiguration: fail loudly rather than fall back silently.
        raise RuntimeError("S3_ACCESS_KEY and S3_SECRET_KEY must be set together (or both empty)")
    return kwargs


def _client():  # type: ignore[no-untyped-def]
    return boto3.client("s3", **_client_kwargs())


def _ensure_bucket(client, bucket: str) -> None:  # type: ignore[no-untyped-def]
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)


def _put_sync(key: str, data: bytes) -> None:
    s = get_settings()
    client = _client()
    _ensure_bucket(client, s.s3_bucket)
    client.put_object(Bucket=s.s3_bucket, Key=key, Body=data)


def _get_sync(key: str) -> bytes | None:
    s = get_settings()
    try:
        resp = _client().get_object(Bucket=s.s3_bucket, Key=key)
        return resp["Body"].read()
    except ClientError:
        return None


def _delete_sync(key: str) -> None:
    # Idempotent in S3: deleting a missing key succeeds.
    _client().delete_object(Bucket=get_settings().s3_bucket, Key=key)  # type: ignore[no-untyped-call]


async def put_object(key: str, data: bytes) -> None:
    await asyncio.to_thread(_put_sync, key, data)


async def get_object(key: str) -> bytes | None:
    return await asyncio.to_thread(_get_sync, key)


async def delete_object(key: str) -> None:
    await asyncio.to_thread(_delete_sync, key)


def state_key(environment_id: str, version_id: str) -> str:
    return f"states/{environment_id}/{version_id}.tfstate"
