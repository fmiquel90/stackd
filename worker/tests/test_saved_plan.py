"""The saved plan (SPECS §7.4): the plan job uploads the exact plan file it showed, before any
after_plan hook; the apply job applies exactly the downloaded file — never a fresh plan — and fails
closed when it is missing, unreadable or stale."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import agent.main as main
from agent.client import ApiClient
from agent.workspace import Workspace

PLAN = b"PK\x03\x04 the reviewed plan \x00\xff"
STALE = {
    "@level": "error",
    "@message": "Error: Saved plan is stale",
    "type": "diagnostic",
    "diagnostic": {
        "severity": "error",
        "summary": "Saved plan is stale",
        "detail": "The given plan file can no longer be applied because the state was changed by "
        "another operation after the plan was created.",
    },
}


class _FakeClient:
    def __init__(self, saved_plan: bytes | Exception = PLAN) -> None:
        self.events: list[tuple[str, dict]] = []
        self.uploads: dict[str, bytes] = {}
        self.lines: list[str] = []
        self._saved_plan = saved_plan

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.lines.extend(line["msg"] for line in lines)

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self.uploads[name] = data

    def download_artifact(self, job_id: str, name: str) -> bytes:
        assert name == "plan.tfplan"
        if isinstance(self._saved_plan, Exception):
            raise self._saved_plan
        return self._saved_plan


class _FakeWorkspace(Workspace):
    """A real workspace (write_plan, files on disk) whose clone is a plain directory; the repo's
    hooks are set per test."""

    repo_hooks: dict[str, list[dict]] = {}

    def git_clone(self, *args: Any, **kw: Any) -> Path:
        cwd = self.path / "repo"
        cwd.mkdir(exist_ok=True)
        return cwd

    def head_sha(self) -> str:
        return "c" * 40

    def load_stackd_yml(self, cwd: Path) -> dict[str, list[dict]]:
        return {
            stage: [{**h, "name": f"repo-{stage}", "source": "repo"} for h in hooks]
            for stage, hooks in self.repo_hooks.items()
        }

    def cleanup(self) -> None:
        pass  # kept so a test can inspect the workspace afterwards


def _hook(command: str) -> list[dict]:
    return [{"command": command}]


def _job(phase: str, **extra: Any) -> dict:
    return {
        "job_id": "run-1",
        "phase": phase,
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "branch": "main",
            "project_root": ".",
        },
        "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        **extra,
    }


def _patch(
    monkeypatch, tofu_calls: list[list[str]], plan_rc: int = 2, apply_result: Any = None
) -> dict[str, bytes]:
    """tofu faked: `plan` writes PLAN where `-out=` says; `apply` records the bytes of the plan file
    it was given, at that moment (returned dict, key "bytes"), and returns `apply_result`."""
    applied: dict[str, bytes] = {}

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple:
        tofu_calls.append(cmd)
        if cmd[1] == "plan":
            out = next(a.removeprefix("-out=") for a in cmd if a.startswith("-out="))
            (cwd / out).write_bytes(PLAN)
            return plan_rc, []
        applied["bytes"] = (cwd / cmd[-1]).read_bytes()
        return apply_result or (0, [])

    monkeypatch.setattr(main, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(main, "run_command", lambda cmd, *a, **kw: tofu_calls.append(cmd) or 0)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(
        main.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "{}", "")
    )
    return applied


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=str(tmp_path), leak_action="warn")


# --- plan job ----------------------------------------------------------------------------------


def test_plan_job_uploads_the_plan_it_showed_before_after_plan_hooks(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    _patch(monkeypatch, calls)
    # A repo after_plan hook overwrites the plan file: what gets applied must not change.
    _FakeWorkspace.repo_hooks = {"after_plan": _hook("echo tampered > plan.tfplan")}
    client = _FakeClient()
    main.handle_plan(client, _job("plan", save_plan=True), _settings(tmp_path))
    assert client.events[-1][0] == "phase_finished", client.events
    assert client.uploads["plan.tfplan"] == PLAN
    assert (tmp_path / "run-1" / "repo" / "plan.tfplan").read_bytes() == b"tampered\n"

    # Nothing to save: a run that can never be applied, or a plan without changes.
    for job, rc in ((_job("plan", save_plan=False), 2), (_job("plan", save_plan=True), 0)):
        _patch(monkeypatch, calls, plan_rc=rc)
        client = _FakeClient()
        main.handle_plan(client, job, _settings(tmp_path / f"{rc}"))
        assert "plan.tfplan" not in client.uploads


def test_destroy_run_plans_with_destroy(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    _patch(monkeypatch, calls)
    _FakeWorkspace.repo_hooks = {}
    main.handle_plan(_FakeClient(), _job("plan", destroy=True, save_plan=True), _settings(tmp_path))
    [plan] = [c for c in calls if c[1] == "plan"]
    assert "-destroy" in plan and f"-out={tmp_path / 'run-1' / 'plan.tfplan'}" in plan


# --- apply job ---------------------------------------------------------------------------------


def test_apply_job_applies_exactly_the_saved_plan(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    applied = _patch(monkeypatch, calls)
    _FakeWorkspace.repo_hooks = {
        # A before_apply repo hook can't swap the plan: it is written after the hook ran…
        "before_apply": _hook("echo tampered > plan.tfplan"),
        # …and an after_apply repo hook never sees it (it holds the run's secrets).
        "after_apply": _hook("test -e plan.tfplan && echo PLAN-PRESENT || echo PLAN-ABSENT"),
    }
    client = _FakeClient()
    main.handle_apply(client, _job("apply"), _settings(tmp_path))
    assert client.events[-1][0] == "phase_finished", client.events

    [apply] = [c for c in calls if c[1] == "apply"]
    assert apply == ["tofu", "apply", "-input=false", "-json", "plan.tfplan"]  # no -auto-approve
    assert applied["bytes"] == PLAN
    assert not any(c[1] == "plan" for c in calls)  # never a fresh plan
    assert "PLAN-ABSENT" in client.lines and "PLAN-PRESENT" not in client.lines


@pytest.mark.parametrize(
    ("saved_plan", "reason"),
    [
        (RuntimeError("HTTP 404: This run has no saved plan to apply"), "HTTP 404: This run has"),
        (RuntimeError("HTTP 500: integrity check failed"), "HTTP 500: integrity check failed"),
        (httpx.ConnectError("connection refused"), "connection refused"),
        (b"", "the plan file is empty"),
    ],
)
def test_apply_fails_closed_without_a_usable_saved_plan(
    monkeypatch, tmp_path: Path, saved_plan: bytes | Exception, reason: str
) -> None:
    calls: list[list[str]] = []
    _patch(monkeypatch, calls)
    _FakeWorkspace.repo_hooks = {"before_apply": _hook("echo BEFORE-APPLY-RAN")}
    client = _FakeClient(saved_plan)
    main.handle_apply(client, _job("apply"), _settings(tmp_path))

    [(kind, kw)] = client.events
    assert kind == "job_failed"
    assert kw["result"]["error"].startswith("saved plan unavailable, not applied: ")
    assert reason in kw["result"]["error"]
    assert [c[1] for c in calls] == ["init"]  # no apply, no re-plan
    assert "BEFORE-APPLY-RAN" not in client.lines


def test_stale_saved_plan_is_a_clear_failure(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    _patch(monkeypatch, calls, apply_result=(1, [STALE]))
    _FakeWorkspace.repo_hooks = {}
    client = _FakeClient()
    main.handle_apply(client, _job("apply"), _settings(tmp_path))
    kind, kw = client.events[-1]
    assert kind == "job_failed"
    assert kw["result"]["error"] == (
        "Saved plan is stale: The given plan file can no longer be applied because the state was "
        "changed by another operation after the plan was created. The reviewed plan was not "
        "applied; trigger a new plan."
    )
    assert not (tmp_path / "run-1" / "repo" / "plan.tfplan").exists()


def test_write_plan_never_writes_through_a_checked_out_symlink(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "job")
    cwd = ws.path / "repo"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (cwd / "plan.tfplan").symlink_to(outside)  # a malicious repo commits this
    path = ws.write_plan(cwd, PLAN)
    assert outside.read_bytes() == b"keep"
    assert not path.is_symlink() and path.read_bytes() == PLAN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_client_download_carries_the_api_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/run-ok/artifacts/plan.tfplan"):
            return httpx.Response(200, content=PLAN)
        return httpx.Response(
            404, json={"title": "No saved plan", "detail": "This run has no saved plan to apply."}
        )

    client = ApiClient("http://api")
    client._http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    client._worker_token = "w"
    assert client.download_artifact("run-ok", "plan.tfplan") == PLAN
    with pytest.raises(RuntimeError, match=r"^HTTP 404: This run has no saved plan to apply\.$"):
        client.download_artifact("run-gone", "plan.tfplan")
