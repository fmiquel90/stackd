"""A whole plan → apply on the `docker` runner (SPECS §7.4), with a real tofu inside the runner
image: the agent clones, every tofu command and hook runs in its own container, the saved plan
goes up and comes back. Skipped without a Docker daemon or without the tofu test image
(`STACKD_TEST_TOFU_IMAGE`, default `stackd-test/tofu:1.12.2`: any image with `sh` and a `tofu` on
its PATH — e.g. alpine plus the release binary)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import agent.main as main
from agent.docker_engine import DockerEngine
from agent.docker_runner import DockerLimits

IMAGE = os.environ.get("STACKD_TEST_TOFU_IMAGE", "stackd-test/tofu:1.12.2")

MAIN_TF = """
resource "terraform_data" "x" {
  input = var.greeting
}
variable "greeting" {
  type = string
}
output "greeting" {
  value = terraform_data.x.output
}
"""

STACKD_YML = """
hooks:
  after_plan:
    - name: repo-peek
      command: "test ! -e ../oidc_token && test ! -e ../plan.tfplan && echo repo-hook-isolated"
      on_failure: fail
"""


class _Client:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.uploads: dict[str, bytes] = {}
        self.lines: list[str] = []

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.lines.extend(line["msg"] for line in lines)

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self.uploads[name] = data

    def download_artifact(self, job_id: str, name: str) -> bytes:
        return self.uploads[name]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, str]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.tf").write_text(MAIN_TF)
    (src / ".stackd.yml").write_text(STACKD_YML)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", ".")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return f"file://{src}", _git(src, "rev-parse", "HEAD")


def _engine() -> None:
    engine = DockerEngine()
    try:
        engine.ping()
    except (httpx.HTTPError, OSError) as exc:
        pytest.skip(f"no Docker daemon: {exc}")
    if not engine.image_exists(IMAGE):
        pytest.skip(f"tofu test image {IMAGE} not present")


@pytest.mark.docker
def test_plan_and_apply_in_containers(tmp_path: Path, repo: tuple[str, str]) -> None:
    _engine()
    url, sha = repo
    settings = SimpleNamespace(
        runner="docker",
        workspace_root=str(tmp_path / "ws"),
        leak_action="warn",
        runner_images={"opentofu:1.12.2": IMAGE},
        available_tools=lambda: {"opentofu": ["1.12.2"]},
        docker_host=None,
        docker_limits=DockerLimits(network="none"),  # terraform_data needs no network
    )

    def job(phase: str, **extra: Any) -> dict:
        return {
            "job_id": "run-docker-1",
            "phase": phase,
            "save_plan": phase == "plan",
            "environment": {
                "tool": "opentofu",
                "tool_version": "1.12",
                "repo_url": url,
                "commit_sha": sha,
                "branch": "main",
                "project_root": ".",
            },
            "env": {},
            "sensitive_env": {},
            "tfvars_json": {"greeting": "hello-from-a-container"},
            "hooks": {
                "after_plan": [
                    {
                        "name": "platform-check",
                        "command": "test -s plan.json && echo platform-hook-ran",
                        "on_failure": "fail",
                        "source": "platform",
                    }
                ]
            },
            "cloud_credentials": {"provider": "aws", "oidc_token": "tok", "role_arn": "arn:x"},
            **extra,
        }

    client = _Client()
    main._run_job(client, job("plan"), settings)  # type: ignore[arg-type]
    kinds = [k for k, _ in client.events]
    assert "job_failed" not in kinds, client.events
    assert ("phase_finished" in kinds) and client.uploads.get("plan.tfplan")
    assert "repo-hook-isolated" in client.lines and "platform-hook-ran" in client.lines
    assert b"hello-from-a-container" in client.uploads["plan.json"]

    client.events.clear()
    main._run_job(client, job("apply"), settings)  # type: ignore[arg-type]
    finished = [kw for k, kw in client.events if k == "phase_finished"]
    assert finished, client.events
    assert finished[-1]["result"]["outputs"]["greeting"]["value"] == "hello-from-a-container"
