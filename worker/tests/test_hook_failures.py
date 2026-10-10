"""Hook failures (SPECS §8.3), at every stage and from either source. A failing `on_failure: fail`
hook fails the job at that stage, before the next tofu step: a failing before_apply hook means
nothing is applied. A `warn` hook lets the job go on and is reported in its checks. The hooks run
for real (`sh -c`); tofu is faked."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import agent.main as main
from agent.client import ApiClient
from agent.config import Settings
from agent.workspace import Workspace

OUTPUTS = {"vpc_id": {"sensitive": False, "type": "string", "value": "vpc-0abc"}}

# stage → (handler, the tofu subcommands that ran before the job failed there)
_STAGES = {
    "before_init": ("handle_plan", []),
    "after_init": ("handle_plan", ["init"]),
    "before_plan": ("handle_plan", ["init"]),
    "after_plan": ("handle_plan", ["init", "plan", "show", "show"]),
    "before_apply": ("handle_apply", ["init"]),
    "after_apply": ("handle_apply", ["init", "apply", "output"]),
}


class _Client:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, *args: Any, **kw: Any) -> None:
        pass

    def upload_artifact(self, *args: Any) -> None:
        pass

    def download_artifact(self, *args: Any) -> bytes:
        return b"PK-plan"


class _Workspace(Workspace):
    """A real workspace whose clone is a plain directory; the repo's hooks are set per test."""

    repo_hooks: dict[str, list[dict]] = {}

    def git_clone(self, *args: Any, **kw: Any) -> Path:
        (self.path / "repo").mkdir(exist_ok=True)
        return self.path / "repo"

    def head_sha(self) -> str:
        return "c" * 40

    def load_stackd_yml(self, cwd: Path) -> dict[str, list[dict]]:
        return {
            stage: [{**h, "source": "repo"} for h in hs] for stage, hs in self.repo_hooks.items()
        }


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    source: str,
    on_failure: str,
    command: str = "exit 3",
) -> tuple[_Client, list[str]]:
    """Run the job holding `stage`, with one hook there running `command`. Returns the client and
    the tofu subcommands that ran."""
    tofu: list[str] = []

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple:
        tofu.append(cmd[1])
        if cmd[1] == "plan":
            out = next(a.removeprefix("-out=") for a in cmd if a.startswith("-out="))
            Path(out).write_bytes(b"PK-plan")
            return 2, []
        return 0, []

    def fake_subprocess_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        tofu.append(cmd[1])
        stdout = json.dumps(OUTPUTS) if cmd[1] == "output" else "{}"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(main, "Workspace", _Workspace)

    def fake_run_command(cmd: list[str], *args: Any, **kw: Any) -> int:
        tofu.append(cmd[1])
        return 0

    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(main.subprocess, "run", fake_subprocess_run)

    hook = {"name": "check", "command": command, "on_failure": on_failure}
    _Workspace.repo_hooks = {stage: [hook]} if source == "repo" else {}
    handler, _ = _STAGES[stage]
    job = {
        "job_id": "run-1",
        "phase": "plan" if handler == "handle_plan" else "apply",
        "save_plan": True,
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "branch": "main",
            "project_root": ".",
        },
        "hooks": {stage: [{**hook, "source": "platform"}]} if source == "platform" else {},
    }
    client = _Client()
    settings = SimpleNamespace(workspace_root=str(tmp_path), leak_action="warn")
    getattr(main, handler)(cast(ApiClient, client), job, cast(Settings, settings))
    return client, tofu


@pytest.mark.parametrize("source", ["platform", "repo"])
@pytest.mark.parametrize("stage", list(_STAGES))
def test_a_failing_hook_fails_the_job_at_its_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str, source: str
) -> None:
    client, tofu = _run(monkeypatch, tmp_path, stage, source, on_failure="fail")
    failed = {"name": "check", "status": "fail", "detail": "exit 3"}
    # After the apply, the infra has changed: its outputs are reported with the failure.
    outputs = {"outputs": OUTPUTS} if stage == "after_apply" else {}
    expected = {"phase": stage, "result": {"checks": [failed], **outputs}}
    assert client.events[-1] == ("job_failed", expected)
    assert tofu == _STAGES[stage][1]  # nothing ran past the failing stage


def test_a_failing_before_apply_hook_stops_the_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _client, tofu = _run(monkeypatch, tmp_path, "before_apply", "repo", on_failure="fail")
    assert "apply" not in tofu


@pytest.mark.parametrize("source", ["platform", "repo"])
@pytest.mark.parametrize("stage", list(_STAGES))
def test_a_warning_hook_lets_the_job_finish_and_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str, source: str
) -> None:
    client, _tofu = _run(monkeypatch, tmp_path, stage, source, on_failure="warn")
    kind, kw = client.events[-1]
    assert kind == "phase_finished", client.events
    assert kw["result"]["checks"] == [{"name": "check", "status": "warn", "detail": "exit 3"}]


@pytest.mark.parametrize("on_failure", ["fail", "warn"])
@pytest.mark.parametrize("source", ["platform", "repo"])
def test_a_hook_past_its_timeout_is_killed_and_fails_by_its_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, on_failure: str
) -> None:
    # §8.3: a hook has its own timeout (10 min by default). Its child process (`sleep`) is killed
    # with it: it would otherwise keep the hook's output open until it ends.
    monkeypatch.setenv("STACKD_HOOK_TIMEOUT_SECONDS", "1")
    started = time.monotonic()
    client, tofu = _run(
        monkeypatch, tmp_path, "before_apply", source, on_failure, "sleep 5; echo LATE"
    )
    assert time.monotonic() - started < 4
    check = {"name": "check", "status": on_failure, "detail": "timed out after 1s"}
    if on_failure == "fail":
        assert client.events[-1] == (
            "job_failed",
            {"phase": "before_apply", "result": {"checks": [check]}},
        )
        assert "apply" not in tofu
    else:
        kind, kw = client.events[-1]
        assert kind == "phase_finished" and kw["result"]["checks"] == [check]
