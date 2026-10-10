"""tofu exit codes fail closed (SPECS §7.4). With -detailed-exitcode a plan succeeds only on 0 (no
changes) or 2 (changes): an error, the watchdog's kill (124), a signal (a negative code) or any
other code fails the job — it never passes for "no changes". So does a plan whose JSON document
(`show -json`, what the after_plan checks read) can't be had. An apply or a command fails on any
non-zero code."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import agent.main as main
from agent.client import ApiClient
from agent.config import Settings
from agent.runner import _DEFAULT_TIMEOUT_SECONDS
from agent.workspace import Workspace


class _Client:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.uploads: dict[str, bytes] = {}

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, *args: Any, **kw: Any) -> None:
        pass

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self.uploads[name] = data

    def download_artifact(self, *args: Any) -> bytes:
        return b"PK-plan"


class _Workspace(Workspace):
    def git_clone(self, *args: Any, **kw: Any) -> Path:
        (self.path / "repo").mkdir(exist_ok=True)
        return self.path / "repo"

    def head_sha(self) -> str:
        return "c" * 40

    def load_stackd_yml(self, cwd: Path) -> dict[str, list[dict]]:
        return {}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    rc: int,
    show_json: tuple[int, str, str] = (0, "{}", ""),
) -> tuple[_Client, list[str]]:
    """Run the `phase` job with tofu faked: init succeeds, the job's main command (plan, apply or
    the command run's subcommand) exits `rc`, `show -json` gives `show_json` (exit code, stdout,
    stderr). Returns the client and the tofu subcommands run."""
    tofu: list[str] = []

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple:
        tofu.append(cmd[1])
        if cmd[1] == "plan":
            out = next(a.removeprefix("-out=") for a in cmd if a.startswith("-out="))
            Path(out).write_bytes(b"PK-plan")
        return rc, []

    def fake_run_command(cmd: list[str], *args: Any, **kw: Any) -> int:
        tofu.append(cmd[1])
        return 0 if cmd[1] in ("init", "show") else rc

    def fake_subprocess_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        tofu.append(cmd[1])
        if cmd[1:3] == ["show", "-json"]:
            return subprocess.CompletedProcess(cmd, *show_json)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(main, "Workspace", _Workspace)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main.subprocess, "run", fake_subprocess_run)
    job = {
        "job_id": "run-1",
        "phase": phase,
        "cred_phase": "apply",
        "save_plan": True,
        "apply_timeout_seconds": 900,
        "command": {"name": "refresh", "args": []},
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "branch": "main",
            "project_root": ".",
        },
    }
    handler = {
        "plan": main.handle_plan,
        "apply": main.handle_apply,
        "command": main.handle_command_run,
    }
    client = _Client()
    settings = SimpleNamespace(workspace_root=str(tmp_path), leak_action="warn")
    handler[phase](cast(ApiClient, client), job, cast(Settings, settings))
    return client, tofu


@pytest.mark.parametrize(
    ("rc", "error"),
    [
        (1, "plan failed"),
        (124, f"plan exceeded the {_DEFAULT_TIMEOUT_SECONDS}s time budget and was terminated"),
        (-9, "plan was killed by SIGKILL"),  # e.g. the OOM killer
        (3, "plan failed (exit 3)"),
    ],
)
def test_a_plan_that_did_not_exit_0_or_2_fails_the_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rc: int, error: str
) -> None:
    client, tofu = _run(monkeypatch, tmp_path, "plan", rc)
    assert client.events[-1] == (
        "job_failed",
        {"phase": "plan", "result": {"error": error, "checks": []}},
    )
    assert not any(kind == "phase_finished" for kind, _ in client.events)
    assert "plan.tfplan" not in client.uploads
    assert tofu == ["init", "plan"]  # not even shown


@pytest.mark.parametrize(("rc", "has_changes"), [(0, False), (2, True)])
def test_a_plan_exiting_0_or_2_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rc: int, has_changes: bool
) -> None:
    client, _tofu = _run(monkeypatch, tmp_path, "plan", rc)
    kind, kw = client.events[-1]
    assert kind == "phase_finished" and kw["result"]["has_changes"] is has_changes


@pytest.mark.parametrize("phase", ["apply", "command"])
@pytest.mark.parametrize("rc", [124, -9, 3])
def test_an_apply_or_command_that_did_not_exit_0_fails_the_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str, rc: int
) -> None:
    client, _tofu = _run(monkeypatch, tmp_path, phase, rc)
    assert client.events[-1][0] == "job_failed", client.events


@pytest.mark.parametrize(
    ("show_json", "reason"),
    [
        (
            (1, "", "\nError: Failed to read the given file as a state or plan file\n\n…"),
            "exited 1: Error: Failed to read the given file as a state or plan file",
        ),
        ((-9, "", ""), "exited -9"),
        ((0, "", ""), "printed no JSON"),
        ((0, '{"format_version": "1.2", "resource_chan', ""), "printed no JSON"),
        ((0, "[]", ""), "printed no plan object"),
    ],
)
def test_a_plan_without_its_json_document_fails_the_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    show_json: tuple[int, str, str],
    reason: str,
) -> None:
    # The after_plan checks would otherwise read `{}` — and pass on an empty plan.
    client, _tofu = _run(monkeypatch, tmp_path, "plan", 2, show_json)
    error = f"plan JSON unavailable, not checked: `tofu show -json` {reason}"
    assert client.events[-1] == (
        "job_failed",
        {"phase": "plan", "result": {"error": error, "checks": []}},
    )
    assert client.uploads == {}  # neither plan.json nor the saved plan
