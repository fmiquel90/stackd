"""A job that changed the infrastructure reports it (SPECS §7.3): an apply that went through, or a
command that ran, is never reported as `job_failed` because its report did not get through. The
report is retried while the apply window lasts (§3.7), then left to the API."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

import agent.main as main
from agent.client import ApiClient
from agent.config import Settings
from agent.workspace import Workspace

_REQUEST = httpx.Request("POST", "http://api/worker/v1/jobs/run-1/events")


class _Client:
    def __init__(self, failures: dict[str, list[Exception]]) -> None:
        self.failures = failures  # per event: what its next attempts raise
        self.events: list[tuple[str, dict]] = []
        self.attempts: list[str] = []
        self.uploads: list[str] = []

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.attempts.append(kind)
        pending = self.failures.get(kind) or []
        if pending:
            raise pending.pop(0)
        self.events.append((kind, kw))

    def logs(self, *args: Any, **kw: Any) -> None:
        pass

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self.uploads.append(name)
        raise httpx.ConnectError("API unreachable", request=_REQUEST)

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


def _down() -> Exception:
    return httpx.ConnectError("API unreachable", request=_REQUEST)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    client: _Client,
    window: float = 30.0,
) -> float:
    """Run a successful `phase` job (tofu faked) through `_run_job`, its apply window `window`
    seconds long. Returns how long it took."""
    monkeypatch.setattr(main, "Workspace", _Workspace)
    monkeypatch.setattr(main, "stream_json_command", lambda *a, **kw: (0, []))
    monkeypatch.setattr(main, "run_command", lambda *a, **kw: 0)
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '{"ip": {"value": "1"}}', ""),
    )
    monkeypatch.setattr(main, "_REPORT_RETRY_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(main, "_apply_window_end", lambda job: time.monotonic() + window)
    job = {
        "job_id": "run-1",
        "phase": phase,
        "cred_phase": "apply",
        "apply_timeout_seconds": 900,
        "command": {"name": "refresh", "args": []},
        "environment": {
            "tool": "opentofu",
            "tool_version": "1.12.2",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "branch": "main",
            "project_root": ".",
        },
    }
    settings = SimpleNamespace(
        runner="local",
        workspace_root=str(tmp_path),
        leak_action="warn",
        available_tools=lambda: {"opentofu": ["1.12.2"]},
    )
    started = time.monotonic()
    main._run_job(cast(ApiClient, client), job, cast(Settings, settings))
    return time.monotonic() - started


def test_a_successful_apply_report_is_retried_until_it_gets_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _Client({"phase_finished": [_down(), _down(), _down()]})
    _run(monkeypatch, tmp_path, "apply", client)
    assert client.events == [
        ("phase_finished", {"result": {"outputs": {"ip": {"value": "1"}}, "checks": []}})
    ]
    assert client.attempts.count("phase_finished") == 4
    assert client.uploads == ["outputs.json"]  # its failure did not fail the job


def test_a_successful_apply_is_never_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API stays unreachable past the apply window: the agent gives up the report, and sends
    no `job_failed` — the apply went through; the run is left to the API's reclaim."""
    client = _Client({"phase_finished": [_down() for _ in range(100_000)]})
    took = _run(monkeypatch, tmp_path, "apply", client, window=0.3)
    assert "job_failed" not in client.attempts
    assert client.events == []
    assert client.attempts.count("phase_finished") > 1  # retried, within the window
    assert 0.3 <= took < 5


def test_a_refused_apply_report_is_not_retried_nor_turned_into_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    conflict = httpx.HTTPStatusError(
        "409", request=_REQUEST, response=httpx.Response(409, request=_REQUEST)
    )
    client = _Client({"phase_finished": [conflict]})
    _run(monkeypatch, tmp_path, "apply", client)
    assert client.attempts == ["phase_finished"]


def test_a_command_that_ran_is_never_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _Client({"phase_finished": [_down(), _down()]})
    _run(monkeypatch, tmp_path, "command", client)
    assert [kind for kind, _ in client.events] == ["phase_started", "phase_finished"]
    assert "job_failed" not in client.attempts
