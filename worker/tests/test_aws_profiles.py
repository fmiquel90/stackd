"""Named AWS profiles (SPECS §10.6): a job holding OIDC credentials gets a platform-generated AWS
config file — `[default]` mirroring the default role, one `[profile <name>]` per named profile,
each assumed with the job's token — on disk only while tofu or a platform hook runs, and the only
AWS config any of them reads."""

from __future__ import annotations

import configparser
import os
from pathlib import Path

import pytest

from agent.main import _cloud_files, _run_env
from agent.workspace import Workspace

ROLE = "arn:aws:iam::123456789012:role/stackd-plan"
STATE_ROLE = "arn:aws:iam::210987654321:role/tfstates-plan"
ADMIN_ROLE = "arn:aws:iam::333333333333:role/prod-admin-plan"
CLOUD = {
    "provider": "aws",
    "oidc_token": "eyJ-workload",
    "role_arn": ROLE,
    "region": "eu-west-1",
    "profiles": {
        "x-tfstates": {"role_arn": STATE_ROLE, "region": None},
        "x-prod.admin": {"role_arn": ADMIN_ROLE, "region": "us-east-1"},
    },
}


def _job(cloud: dict | None = CLOUD, **extra: object) -> dict:
    return {"job_id": "run-1", "env": {}, "sensitive_env": {}, "cloud_credentials": cloud, **extra}


def test_config_file_holds_default_and_named_profiles(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "run-1")
    job = _job()
    env = _run_env(ws, job)
    token = str(ws.secret_path("oidc_token"))
    config = Path(env["AWS_CONFIG_FILE"])
    assert config == ws.secret_path("aws_config")
    assert not config.is_relative_to(ws.path / "repo")  # outside the checkout

    with _cloud_files(ws, job):
        text = config.read_text()
        assert oct(config.stat().st_mode & 0o777) == "0o600"
    assert text == (
        "[default]\n"
        f"role_arn = {ROLE}\n"
        f"web_identity_token_file = {token}\n"
        "role_session_name = stackd-run-1\n"
        "region = eu-west-1\n"
        "\n"
        "[profile x-prod.admin]\n"
        f"role_arn = {ADMIN_ROLE}\n"
        f"web_identity_token_file = {token}\n"
        "role_session_name = stackd-run-1\n"
        "region = us-east-1\n"
        "\n"
        "[profile x-tfstates]\n"
        f"role_arn = {STATE_ROLE}\n"
        f"web_identity_token_file = {token}\n"
        "role_session_name = stackd-run-1\n"
    )
    # An INI file the AWS SDKs read (botocore parses it with configparser).
    parsed = configparser.ConfigParser()
    parsed.read_string(text)
    assert parsed.sections() == ["default", "profile x-prod.admin", "profile x-tfstates"]
    assert parsed["profile x-tfstates"]["role_arn"] == STATE_ROLE


def test_without_named_profiles_the_file_has_the_default_section_only(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "run-1")
    job = _job({"provider": "aws", "oidc_token": "t", "role_arn": ROLE, "region": None})
    env = _run_env(ws, job)
    with _cloud_files(ws, job):
        text = Path(env["AWS_CONFIG_FILE"]).read_text()
    assert text.startswith("[default]\n") and "[profile" not in text and "region" not in text


def test_files_exist_only_while_the_command_runs(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "run-1")
    config, token = ws.secret_path("aws_config"), ws.secret_path("oidc_token")
    assert not config.exists()
    with _cloud_files(ws, _job()):
        assert config.exists() and token.exists()
    assert not config.exists() and not token.exists()  # gone before any repo hook runs


def test_a_planted_symlink_is_replaced_not_written_through(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "run-1")
    loot = tmp_path / "loot"
    ws.secret_path("aws_config").symlink_to(loot)
    with _cloud_files(ws, _job()):
        pass
    assert not loot.exists()


def test_no_oidc_credentials_no_file_and_no_variable(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "run-1")
    job = _job(None)
    assert "AWS_CONFIG_FILE" not in _run_env(ws, job)
    with _cloud_files(ws, job):
        assert not ws.secret_path("aws_config").exists()


@pytest.mark.parametrize(
    "profiles",
    [
        {"x]\n[evil": {"role_arn": ROLE}},
        {"default": {"role_arn": ROLE}},
        {"with space": {"role_arn": ROLE}},
        {"x": {"role_arn": f"{ROLE}\ncredential_process = /bin/sh"}},
        {"x": {"role_arn": ROLE, "region": "eu-west-1\r\nregion = x"}},
    ],
)
def test_a_value_that_breaks_the_file_fails_the_job(tmp_path: Path, profiles: dict) -> None:
    ws = Workspace(str(tmp_path), "run-1")
    with pytest.raises(RuntimeError):
        _run_env(ws, _job({**CLOUD, "profiles": profiles}))


def test_platform_config_wins_over_every_user_supplied_aws_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10.5 stays: neither the worker host's nor the run's AWS_CONFIG_FILE / AWS_PROFILE /
    credentials file reaches tofu — only the platform's file; AWS_PROFILE is never set."""
    monkeypatch.setenv("AWS_CONFIG_FILE", "/home/me/.aws/config")
    monkeypatch.setenv("AWS_PROFILE", "host-admin")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/home/me/.aws/credentials")
    ws = Workspace(str(tmp_path), "run-1")
    job = _job(
        env={"AWS_CONFIG_FILE": "/repo/aws.cfg", "AWS_PROFILE": "x-tfstates", "AWS_REGION": "x"},
        sensitive_env={"AWS_DEFAULT_PROFILE": "y", "AWS_ACCESS_KEY_ID": "AKIA"},
    )
    env = _run_env(ws, job)
    assert env["AWS_CONFIG_FILE"] == str(ws.secret_path("aws_config"))
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID"):
        assert name not in env
    assert env["AWS_REGION"] == "eu-west-1"  # the integration's region, set last
    assert env["AWS_ROLE_ARN"] == ROLE


def test_a_run_region_stays_when_the_integration_sets_none(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "run-1")
    env = _run_env(ws, _job({**CLOUD, "region": None}, env={"AWS_REGION": "eu-central-1"}))
    assert env["AWS_REGION"] == "eu-central-1"
