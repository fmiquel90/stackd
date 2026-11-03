from __future__ import annotations

import pytest

from app.config import get_settings
from app.statebackend import store


@pytest.fixture
def s3_settings(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def _apply(**env: str) -> None:
        for key in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT_URL"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield _apply
    get_settings.cache_clear()


def test_no_static_keys_uses_the_default_credential_chain(s3_settings) -> None:  # type: ignore[no-untyped-def]
    # Production on AWS: no keys configured → boto3 must resolve the task role, never a dummy key.
    s3_settings(AWS_REGION="eu-west-1")
    kwargs = store._client_kwargs()
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["endpoint_url"] is None


def test_explicit_keys_are_used_for_a_custom_endpoint(s3_settings) -> None:  # type: ignore[no-untyped-def]
    # Dev Garage (or any S3-compatible store): the configured key pair and endpoint are kept.
    s3_settings(S3_ENDPOINT_URL="http://garage:3900", S3_ACCESS_KEY="GKabc", S3_SECRET_KEY="secret")
    kwargs = store._client_kwargs()
    assert kwargs["aws_access_key_id"] == "GKabc"
    assert kwargs["aws_secret_access_key"] == "secret"
    assert kwargs["endpoint_url"] == "http://garage:3900"


def test_half_a_key_pair_is_refused(s3_settings) -> None:  # type: ignore[no-untyped-def]
    s3_settings(S3_ACCESS_KEY="GKabc")
    with pytest.raises(RuntimeError, match="must be set together"):
        store._client_kwargs()


def test_client_signs_with_the_chain_credentials(s3_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # End to end through boto3: the client's credentials come from the environment chain.
    s3_settings(AWS_REGION="us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIACHAIN")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "chain-secret")
    creds = store._client()._request_signer._credentials
    assert creds.access_key == "AKIACHAIN"
