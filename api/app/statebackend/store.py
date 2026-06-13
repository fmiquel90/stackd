from __future__ import annotations

import asyncio

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings

# S3 holds the bytes; the API is the only thing with credentials to it (SPECS §11.1).


def _client():  # type: ignore[no-untyped-def]
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        aws_access_key_id=s.s3_access_key or "test",
        aws_secret_access_key=s.s3_secret_key or "test",
        region_name=s.aws_region or "us-east-1",
    )


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


async def put_object(key: str, data: bytes) -> None:
    await asyncio.to_thread(_put_sync, key, data)


async def get_object(key: str) -> bytes | None:
    return await asyncio.to_thread(_get_sync, key)


def state_key(environment_id: str, version_id: str) -> str:
    return f"states/{environment_id}/{version_id}.tfstate"
