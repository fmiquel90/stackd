"""Out-of-band state change detection (SPECS §19.7): with `observe_state`, the agent reads the state
through `state pull` and reports its `serial` and `lineage` only — never anything else of it, never
on disk nor in a log — before the plan / apply / command (a check) and after the apply / command (a
baseline). A failed read is a warning: it never fails the job."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import agent.main as main
from agent.workspace import Workspace

SECRET = "s3cr3t-db-password"
LINEAGE = "0b9c2f1e-7d4a-4f3e-9a51-6c2b8e0d1a77"
STATE = json.dumps(
    {
        "version": 4,
        "terraform_version": "1.9.0",
        "serial": 42,
        "lineage": LINEAGE,
        "outputs": {"db_password": {"value": SECRET, "type": "string", "sensitive": True}},
        "resources": [{"type": "aws_db_instance", "instances": [{"attributes": {"pw": SECRET}}]}],
    }
)
PLAN = b"PK\x03\x04 plan"


class _FakeClient:
    def __init__(self, refuse_observation: bool = False) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lines: list[str] = []
        self.uploads: dict[str, bytes] = {}
        self._refuse = refuse_observation

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        if kind == "state_observed" and self._refuse:
            request = httpx.Request("POST", "http://api/worker/v1/jobs/run-1/events")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError(
                "409 State not observed", request=request, response=response
            )
        self.events.append((kind, kw))

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.lines.extend(line["msg"] for line in lines)

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self.uploads[name] = data

    def download_artifact(self, job_id: str, name: str) -> bytes:
        return PLAN

    def observations(self) -> list[dict]:
        return [kw["result"] for kind, kw in self.events if kind == "state_observed"]


class _FakeWorkspace(Workspace):
    def git_clone(self, *args: Any, **kw: Any) -> Path:
        cwd = self.path / "repo"
        cwd.mkdir(exist_ok=True)
        return cwd

    def head_sha(self) -> str:
        return "c" * 40

    def load_stackd_yml(self, cwd: Path) -> dict[str, list[dict]]:
        return {}

    def cleanup(self) -> None:
        pass  # kept so a test can inspect the workspace afterwards


def _job(phase: str, **extra: Any) -> dict:
    return {
        "job_id": "run-1",
        "phase": phase,
        "observe_state": True,
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
    monkeypatch,
    calls: list[list[str]],
    pulls: list[subprocess.CompletedProcess[str] | Exception],
    apply_rc: int = 0,
) -> None:
    """tofu faked; each `state pull` answers the next of `pulls` (stdout = the state document)."""

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple:
        calls.append(cmd)
        if cmd[1] == "plan":
            out = next(a.removeprefix("-out=") for a in cmd if a.startswith("-out="))
            Path(out).write_bytes(PLAN)
            return 2, []
        return apply_rc, []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[1:3] == ["state", "pull"]:
            assert kw.get("timeout") == main._STATE_PULL_TIMEOUT_SECONDS
            answer = pulls.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(main, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(main, "run_command", lambda cmd, *a, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(main.subprocess, "run", fake_run)


def _pulled(stdout: str, rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["tofu", "state", "pull"], rc, stdout, stderr)


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(runner="local", workspace_root=str(tmp_path), leak_action="warn")


def _assert_no_state_leak(client: _FakeClient, tmp_path: Path) -> None:
    """Nothing of the state but serial/lineage reached the API, the run's log or the disk."""
    sent = json.dumps([client.events, client.lines]) + repr(client.uploads)
    assert SECRET not in sent
    assert '"resources"' not in sent
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="ignore"), path


# --- parsing -----------------------------------------------------------------------------------


def test_state_identity_keeps_serial_and_lineage_only() -> None:
    assert main._state_identity(STATE) == (42, LINEAGE)


def test_empty_state_is_none_none() -> None:
    # No state yet (a first deploy): `state pull` prints nothing.
    assert main._state_identity("") == (None, None)
    assert main._state_identity("\n  \n") == (None, None)


@pytest.mark.parametrize(
    "raw",
    [
        f"not json {SECRET}",
        json.dumps([SECRET]),
        json.dumps({"serial": "42", "lineage": LINEAGE, "x": SECRET}),
        json.dumps({"serial": True, "lineage": LINEAGE}),
        json.dumps({"serial": -1, "lineage": LINEAGE}),
        json.dumps({"serial": 3, "lineage": ""}),
        json.dumps({"serial": 3}),
    ],
)
def test_unparsable_state_is_refused_without_its_content(raw: str) -> None:
    with pytest.raises(main._NoStateIdentity) as err:
        main._state_identity(raw)
    assert SECRET not in str(err.value) and err.value.__cause__ is None


# --- jobs --------------------------------------------------------------------------------------


def test_plan_job_reports_a_check_after_init_before_the_plan(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    _patch(monkeypatch, calls, [_pulled(STATE)])
    client = _FakeClient()
    main.handle_plan(client, _job("plan", save_plan=True), _settings(tmp_path))

    assert client.observations() == [{"serial": 42, "lineage": LINEAGE, "baseline": False}]
    order = [c[1] if c[1] != "state" else "state pull" for c in calls]
    assert order.index("init") < order.index("state pull") < order.index("plan")
    assert client.events[-1][0] == "phase_finished"
    _assert_no_state_leak(client, tmp_path)


def test_absent_state_reports_nulls(monkeypatch, tmp_path: Path) -> None:
    _patch(monkeypatch, [], [_pulled("")])
    client = _FakeClient()
    main.handle_plan(client, _job("plan"), _settings(tmp_path))
    assert client.observations() == [{"serial": None, "lineage": None, "baseline": False}]


def test_no_observation_unless_asked(monkeypatch, tmp_path: Path) -> None:
    # A PR's proposed run, a managed-state env, an older API: no `state pull` at all.
    calls: list[list[str]] = []
    _patch(monkeypatch, calls, [])
    client = _FakeClient()
    main.handle_plan(client, _job("plan", observe_state=False), _settings(tmp_path))
    assert not [c for c in calls if c[1:3] == ["state", "pull"]]
    assert client.observations() == []


@pytest.mark.parametrize(
    "pull",
    [
        _pulled("", rc=1, stderr=f"Error: AccessDenied {SECRET}"),
        _pulled(f"garbage {SECRET}"),
        subprocess.TimeoutExpired(["tofu", "state", "pull"], 120, output=SECRET),
        OSError("exec failed"),
    ],
)
def test_read_failure_warns_and_never_fails_the_job(monkeypatch, tmp_path: Path, pull: Any) -> None:
    _patch(monkeypatch, [], [pull])
    client = _FakeClient()
    main.handle_plan(client, _job("plan"), _settings(tmp_path))
    assert client.observations() == []
    assert client.events[-1][0] == "phase_finished", client.events
    assert any("state serial not read" in line for line in client.lines)
    _assert_no_state_leak(client, tmp_path)


def test_refused_report_never_fails_the_job(monkeypatch, tmp_path: Path) -> None:
    _patch(monkeypatch, [], [_pulled(STATE)])
    client = _FakeClient(refuse_observation=True)
    main.handle_plan(client, _job("plan"), _settings(tmp_path))
    assert client.events[-1][0] == "phase_finished", client.events
    assert any("state serial not read" in line for line in client.lines)


@pytest.mark.parametrize("apply_rc", [0, 1])
def test_apply_job_checks_before_and_records_a_baseline_after(
    monkeypatch, tmp_path: Path, apply_rc: int
) -> None:
    after = STATE.replace('"serial": 42', '"serial": 43')
    calls: list[list[str]] = []
    _patch(monkeypatch, calls, [_pulled(STATE), _pulled(after)], apply_rc=apply_rc)
    client = _FakeClient()
    main.handle_apply(client, _job("apply"), _settings(tmp_path))

    # The baseline is read whatever the apply's exit: a failed apply may have written state.
    assert client.observations() == [
        {"serial": 42, "lineage": LINEAGE, "baseline": False},
        {"serial": 43, "lineage": LINEAGE, "baseline": True},
    ]
    order = [c[1] if c[1] != "state" else "state pull" for c in calls]
    assert order.index("init") < order.index("state pull") < order.index("apply")
    assert order[order.index("apply") + 1] == "state pull"
    assert client.events[-1][0] == ("phase_finished" if apply_rc == 0 else "job_failed")
    _assert_no_state_leak(client, tmp_path)


def test_mutating_command_checks_before_and_records_a_baseline_after(
    monkeypatch, tmp_path: Path
) -> None:
    after = STATE.replace('"serial": 42', '"serial": 43')
    calls: list[list[str]] = []
    _patch(monkeypatch, calls, [_pulled(STATE), _pulled(after)])
    client = _FakeClient()
    job = _job("command", cred_phase="apply", command={"name": "state rm", "args": ["a.b"]})
    main.handle_command_run(client, job, _settings(tmp_path))
    assert [o["baseline"] for o in client.observations()] == [False, True]
    assert [o["serial"] for o in client.observations()] == [42, 43]
    assert client.events[-1][0] == "phase_finished"
