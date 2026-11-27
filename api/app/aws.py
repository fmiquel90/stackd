"""The botocore settings every AWS client of the API is built with (S3, KMS, STS)."""

from __future__ import annotations

from botocore.config import Config  # type: ignore[import-untyped]

from app.config import get_settings


def client_config() -> Config:
    """Short connect/read timeouts and standard-mode retries: botocore's defaults (60 s each,
    legacy retries) hold a request — and whatever waits on it — for minutes when the endpoint
    stalls."""
    s = get_settings()
    return Config(
        connect_timeout=s.stackd_aws_connect_timeout_seconds,
        read_timeout=s.stackd_aws_read_timeout_seconds,
        # `total_`: the first attempt included (botocore's `max_attempts` counts only retries).
        retries={"mode": "standard", "total_max_attempts": s.stackd_aws_max_attempts},
    )
