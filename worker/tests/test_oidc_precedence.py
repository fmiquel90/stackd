"""OIDC credentials are a job's only cloud identity (SPECS §10.5): with them, the worker host's own
AWS credentials (a mounted ~/.aws on the trusted-dev `local` runner) never reach tofu or a platform
hook. Without them, the static fallback (§10.4) is untouched."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.main import _run_env
from agent.workspace import Workspace

JOB = {"job_id": "run-1", "env": {"AWS_REGION": "eu-west-1"}, "sensitive_env": {}}
OIDC = {"provider": "aws", "oidc_token": "tok", "role_arn": "arn:aws:iam::1:role/plan"}


@pytest.fixture
def host_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAHOSTKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-secret")
    monkeypatch.setenv("AWS_PROFILE", "admin")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/root/.aws/credentials")


def test_oidc_job_drops_host_aws_credentials(tmp_path: Path, host_aws: None) -> None:
    ws = Workspace(tmp_path, "run-1")
    env = _run_env(ws, {**JOB, "cloud_credentials": OIDC})
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        assert name not in env
    assert "AWS_SHARED_CREDENTIALS_FILE" not in env
    assert env["AWS_ROLE_ARN"] == OIDC["role_arn"]
    assert env["AWS_REGION"] == "eu-west-1"


def test_static_fallback_keeps_host_aws_credentials(tmp_path: Path, host_aws: None) -> None:
    ws = Workspace(tmp_path, "run-1")
    env = _run_env(ws, {**JOB, "cloud_credentials": None})
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAHOSTKEY"
    assert env["AWS_PROFILE"] == "admin"
