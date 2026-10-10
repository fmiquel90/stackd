"""Hook failures (SPECS §8.3), at every stage and from either source. A failing `on_failure: fail`
hook fails the job at that stage, before the next tofu step: a failing before_apply hook means
nothing is applied. A `warn` hook lets the job go on and is reported in its checks. The hooks run
for real (`sh -c`); tofu is faked."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import agent.main as main
from agent.client import ApiClient
from agent.config import Settings
from agent.runner import LogStreamer, run_hooks
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
    at: list[str] | None = None,
    fail_at: str | None = None,
) -> tuple[_Client, list[str]]:
    """Run the job holding `stage`, with one hook there (or at each stage of `at`) running
    `command`. Returns the client and the tofu subcommands that ran. `fail_at` fails one step
    besides the hooks: a tofu subcommand (`init`, `plan`, `apply`), the plan's `show -json`
    (`show`), or the `plan.json` upload (`upload`, an error the job doesn't catch); the job then
    runs as the agent does (`_run_job`), which reports an uncaught error."""
    tofu: list[str] = []

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple:
        tofu.append(cmd[1])
        if cmd[1] == fail_at:
            return 1, []
        if cmd[1] == "plan":
            out = next(a.removeprefix("-out=") for a in cmd if a.startswith("-out="))
            Path(out).write_bytes(b"PK-plan")
            return 2, []
        return 0, []

    def fake_subprocess_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        tofu.append(cmd[1])
        if fail_at == "show" and cmd[1:3] == ["show", "-json"]:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: unreadable plan")
        stdout = json.dumps(OUTPUTS) if cmd[1] == "output" else "{}"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(main, "Workspace", _Workspace)

    def fake_run_command(cmd: list[str], *args: Any, **kw: Any) -> int:
        tofu.append(cmd[1])
        return 1 if cmd[1] == fail_at else 0

    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(main.subprocess, "run", fake_subprocess_run)

    hook = {"name": "check", "command": command, "on_failure": on_failure}
    hooks = {s: [hook] for s in at or [stage]}
    _Workspace.repo_hooks = hooks if source == "repo" else {}
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
        "hooks": (
            {s: [{**h, "source": "platform"} for h in hs] for s, hs in hooks.items()}
            if source == "platform"
            else {}
        ),
    }
    client = _Client()
    settings = SimpleNamespace(workspace_root=str(tmp_path), leak_action="warn")
    if fail_at is None:
        getattr(main, handler)(cast(ApiClient, client), job, cast(Settings, settings))
        return client, tofu
    if fail_at == "upload":

        def refuse(*args: Any) -> None:
            raise RuntimeError("upload refused: 503")

        monkeypatch.setattr(client, "upload_artifact", refuse)
    main._run_job(cast(ApiClient, client), job, cast(Settings, settings))
    return client, tofu


@pytest.mark.parametrize("source", ["platform", "repo"])
@pytest.mark.parametrize("stage", list(_STAGES))
def test_a_failing_hook_fails_the_job_at_its_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str, source: str
) -> None:
    client, tofu = _run(monkeypatch, tmp_path, stage, source, on_failure="fail")
    failed = {"name": "check", "status": "fail", "detail": "exit 3", "stage": stage}
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
    warned = {"name": "check", "status": "warn", "detail": "exit 3", "stage": stage}
    assert kw["result"]["checks"] == [warned]


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
    check = {
        "name": "check",
        "status": on_failure,
        "detail": "timed out after 1s",
        "stage": "before_apply",
    }
    if on_failure == "fail":
        assert client.events[-1] == (
            "job_failed",
            {"phase": "before_apply", "result": {"checks": [check]}},
        )
        assert "apply" not in tofu
    else:
        kind, kw = client.events[-1]
        assert kind == "phase_finished" and kw["result"]["checks"] == [check]


class _Streamer:
    def emit(self, *args: Any, **kw: Any) -> None:
        pass


def _running(pid: int) -> bool:
    """True while `pid` runs (a killed process its reaper has yet to collect counts as gone)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads /proc")
@pytest.mark.parametrize("source", ["platform", "repo"])
@pytest.mark.parametrize(
    "background", ["sleep 30 &", "sleep 30 >/dev/null 2>&1 &"], ids=["holding-output", "detached"]
)
def test_a_hooks_background_process_does_not_outlive_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, background: str
) -> None:
    # §8.3: once the hook exits, its whole process group is killed — a background process would
    # otherwise still run while tofu has the tfvars and OIDC token on disk, or a platform hook the
    # raw plan JSON. One still holding the hook's output no longer keeps the hook from ending.
    monkeypatch.setenv("STACKD_HOOK_TIMEOUT_SECONDS", "5")
    pid_file = tmp_path / "background.pid"
    hook = {"name": "bg", "command": f"{background} echo $! > {pid_file}", "source": source}
    started = time.monotonic()
    results, aborted = run_hooks(
        [hook],
        tmp_path,
        platform_env=dict(os.environ),
        repo_env=dict(os.environ),
        phase="planning",
        streamer=cast(LogStreamer, _Streamer()),
    )
    pid = int(pid_file.read_text())
    try:
        assert time.monotonic() - started < 3  # not the hook timeout's kill
        assert (results, aborted) == ([{"name": "bg", "status": "ok", "detail": None}], False)
        deadline = time.monotonic() + 2
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _running(pid)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("fail_at", "phase", "ran"),
    [
        ("init", "init", ["before_init"]),
        ("plan", "plan", ["before_init", "after_init", "before_plan"]),
        ("show", "plan", ["before_init", "after_init", "before_plan"]),  # no plan JSON
        ("upload", "plan", ["before_init", "after_init", "before_plan"]),  # an uncaught error
        ("apply", "apply", ["before_apply"]),
    ],
)
def test_a_job_failing_on_no_check_reports_the_checks_it_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_at: str, phase: str, ran: list[str]
) -> None:
    # §8.3: whatever ends the job, its job_failed carries the check results of the stages that
    # ran, for the API to record them and audit a warning among them.
    stages = [s for s, (handler, _) in _STAGES.items() if handler == _STAGES[ran[0]][0]]
    client, _tofu = _run(monkeypatch, tmp_path, ran[0], "repo", "warn", at=stages, fail_at=fail_at)
    kind, kw = client.events[-1]
    assert (kind, kw["phase"]) == ("job_failed", phase), client.events
    assert kw["result"]["error"]
    warned = {"name": "check", "status": "warn", "detail": "exit 3"}
    assert kw["result"]["checks"] == [{**warned, "stage": s} for s in ran]


@pytest.mark.parametrize("phase", ["plan", "apply"])
def test_every_check_result_is_reported_with_its_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    # Passing checks too, so the API records every result and where it came from (§8.3).
    stages = [s for s, (handler, _) in _STAGES.items() if handler == f"handle_{phase}"]
    client, _tofu = _run(monkeypatch, tmp_path, stages[0], "repo", "fail", "true", stages)
    kind, kw = client.events[-1]
    assert kind == "phase_finished", client.events
    ok = {"name": "check", "status": "ok", "detail": None}
    assert kw["result"]["checks"] == [{**ok, "stage": s} for s in stages]
