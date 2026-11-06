from __future__ import annotations

import asyncio

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings
from app.logging import get_logger

_log = get_logger("stackd.statebackend")

# S3 holds the bytes; the API is the only thing with credentials to it (SPECS §11.1).


def _client():  # type: ignore[no-untyped-def]
    s = get_settings()
    if s.s3_access_key or s.s3_secret_key:
        creds = {"aws_access_key_id": s.s3_access_key, "aws_secret_access_key": s.s3_secret_key}
    elif s.is_production:
        # No static keys: the AWS default credential chain (instance profile, ECS task role, IRSA /
        # web identity, environment, shared config) — no long-lived key to store or rotate.
        creds = {}
    else:
        creds = {"aws_access_key_id": "test", "aws_secret_access_key": "test"}  # dev/moto
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        region_name=s.aws_region or "us-east-1",
        **creds,
    )


def _ensure_bucket(client, bucket: str) -> None:  # type: ignore[no-untyped-def]
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)
        _harden_bucket(client, bucket)


def _harden_bucket(client, bucket: str) -> None:  # type: ignore[no-untyped-def]
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
                    "error": exc.response.get("Error", {}).get("Code", "unknown"),
                },
            )


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


def _list_sync(prefix: str) -> list[str]:
    s = get_settings()
    keys: list[str] = []
    try:
        pages = (
            _client().get_paginator("list_objects_v2").paginate(Bucket=s.s3_bucket, Prefix=prefix)
        )
        for page in pages:
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
    except ClientError:
        return []
    return keys


async def list_keys(prefix: str) -> list[str]:
    return await asyncio.to_thread(_list_sync, prefix)


async def put_object(key: str, data: bytes) -> None:
    await asyncio.to_thread(_put_sync, key, data)


async def get_object(key: str) -> bytes | None:
    return await asyncio.to_thread(_get_sync, key)


async def delete_object(key: str) -> None:
    await asyncio.to_thread(_delete_sync, key)


def state_key(environment_id: str, version_id: str) -> str:
    return f"states/{environment_id}/{version_id}.tfstate"
