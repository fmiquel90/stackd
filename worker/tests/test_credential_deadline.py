"""The job's credential deadline (SPECS §7.4): every step's timeout is cut to what is left before
the job's first short-lived credential (state token, OIDC token) expires, and an apply or a
mutating command is not started at all when too little is left — nothing is applied rather than
an apply cut short or sending its last state write with an expired token."""

from __future__ import annotations

import base64
import json
import os
import time
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent.main as main
from agent import deadline as dl
from agent.deadline import Deadline, token_lifetime
from agent.masking import Masker
from agent.runner import _DEFAULT_TIMEOUT_SECONDS, LogStreamer, run_hooks


def _jwt(lifetime: int, iat: int = 1_700_000_000) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256'})}.{part({'iat': iat, 'exp': iat + lifetime})}.sig"


# --- the deadline -------------------------------------------------------------------------------


def test_token_lifetime_reads_exp_minus_iat_only() -> None:
    assert token_lifetime(_jwt(1200)) == 1200
    # Skew-proof: an `iat` far from this host's clock changes nothing.
    assert token_lifetime(_jwt(1200, iat=42)) == 1200
    for bad in ("opaque-token", "a.b.c", "a." + base64.urlsafe_b64encode(b"[]").decode() + ".c"):
        assert token_lifetime(bad) is None


def test_deadline_is_the_first_credential_to_expire_from_the_claim() -> None:
    job = {
        "backend": {"password": _jwt(1200)},  # the apply window
        "cloud_credentials": {"oidc_token": _jwt(900)},  # a shorter session_duration
    }
    assert Deadline.for_job(job, received_at=1000.0).at == 1900.0
    assert Deadline.for_job({"backend": {"password": _jwt(21600)}}, 0.0).at == 21600.0
    # No short-lived credential (unmanaged state, static keys): the steps keep their own budgets.
    none = Deadline.for_job({"backend": None, "cloud_credentials": None}, 0.0)
    assert none.at is None and none.timeout(7200) == 7200 and none.too_short_for(900) is None


def test_an_unreadable_token_fails_closed_to_the_documented_window() -> None:
    apply_job = {"cred_phase": "apply", "apply_timeout_seconds": 900, "backend": {"password": "x"}}
    assert Deadline.for_job(apply_job, 0.0).at == 900 + 300
    plan_job = {"cred_phase": "plan", "backend": {"password": "x"}}
    assert Deadline.for_job(plan_job, 0.0).at == 30 * 60


def test_timeout_is_cut_to_what_is_left_less_the_margin() -> None:
    d = Deadline(time.monotonic() + 600)
    assert 530 <= d.timeout(7200) <= 540  # 600 - 60 s margin
    assert d.timeout(100) == 100  # a shorter own budget stands
    past = Deadline(time.monotonic() - 10)
    assert past.timeout(7200) == dl.MIN_COMMAND_SECONDS


def test_too_short_for_an_apply() -> None:
    assert Deadline(time.monotonic() + 1200).too_short_for(900) is None
    left = Deadline(time.monotonic() + 200).too_short_for(900)
    assert left is not None and 130 <= left <= 140
    # A budget shorter than the floor only needs itself.
    assert Deadline(time.monotonic() + 200).too_short_for(120) is None


# --- hooks ----------------------------------------------------------------------------------------


class _Client:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lines: list[str] = []
        self.uploads: dict[str, bytes] = {}

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.lines.extend(line["msg"] for line in lines)

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self.uploads[name] = data

    def download_artifact(self, job_id: str, name: str) -> bytes:
        return b"PK saved plan"


def test_a_hook_is_cut_to_the_deadline(tmp_path: Path) -> None:
    client = _Client()
    results, aborted = run_hooks(
        [{"name": "slow", "command": "sleep 5", "on_failure": "fail"}],
        tmp_path,
        platform_env={"PATH": os.environ["PATH"]},
        repo_env={},
        phase="applying",
        streamer=LogStreamer(client, "run-1", Masker([])),  # type: ignore[arg-type]
        bound=lambda own: 1,
    )
    assert aborted and results[0]["detail"] == "timed out after 1s"


# --- the jobs -------------------------------------------------------------------------------------


class _Workspace:
    def __init__(self, root: str, job_id: str) -> None:
        self.path = Path(root) / job_id
        (self.path / "repo").mkdir(parents=True, exist_ok=True)

    def git_clone(self, *args: object, **kw: object) -> Path:
        return self.path / "repo"

    def head_sha(self) -> str:
        return "c" * 40

    def repo_credentials(self, *args: object, **kw: object) -> AbstractContextManager[dict]:
        return nullcontext({})

    def tfvars(self, *args: object) -> AbstractContextManager[None]:
        return nullcontext()

    def write_backend_override(self, *args: object) -> None:
        pass

    def load_stackd_yml(self, cwd: Path) -> dict:
        return {}

    def write_plan(self, cwd: Path, data: bytes) -> Path:
        path = cwd / "plan.tfplan"
        path.write_bytes(data)
        return path

    def private_path(self, name: str) -> Path:
        return self.path / name

    def secret_path(self, name: str) -> Path:
        return self.path / name

    def secret_file(self, name: str, content: str) -> AbstractContextManager[None]:
        return nullcontext()

    def cleanup(self) -> None:
        pass


def _job(phase: str, **extra: Any) -> dict:
    return {
        "job_id": "run-1",
        "phase": phase,
        "cred_phase": "plan" if phase == "plan" else "apply",
        "apply_timeout_seconds": 900,
        "environment": {
            "tool": "opentofu",
            "tool_version": "1.12.2",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "branch": "main",
            "project_root": ".",
        },
        **extra,
    }


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        runner="local", workspace_root=str(tmp_path), leak_action="warn", worker_name="w1"
    )


def _fake_tofu(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every tofu command faked, recording (subcommand, timeout)."""
    calls: list[tuple[str, int]] = []

    def run_command(cmd: list[str], *a: Any, **kw: Any) -> int:
        calls.append((cmd[1], kw.get("timeout", _DEFAULT_TIMEOUT_SECONDS)))
        return 0

    def stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple[int, list]:
        calls.append((cmd[1], kw["timeout"]))
        return (2 if cmd[1] == "plan" else 0), []

    def capture(self: Any, cmd: list[str], cwd: Path, env: dict, **kw: Any) -> Any:
        calls.append((cmd[1], kw["timeout"]))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(main, "Workspace", _Workspace)
    monkeypatch.setattr(main, "run_command", run_command)
    monkeypatch.setattr(main, "stream_json_command", stream_json)
    monkeypatch.setattr(type(main.LOCAL), "capture", capture)
    return calls


def test_apply_is_not_started_with_too_little_left(monkeypatch, tmp_path: Path) -> None:
    """The clone, init and before_apply hooks ate the credentials' lifetime: no apply at all."""
    calls = _fake_tofu(monkeypatch)
    client = _Client()
    main.handle_apply(
        client,  # type: ignore[arg-type]
        _job("apply"),
        _settings(tmp_path),  # type: ignore[arg-type]
        deadline=Deadline(time.monotonic() + 200),
    )
    assert "apply" not in [c[0] for c in calls]
    kind, kw = client.events[-1]
    assert kind == "job_failed" and kw["phase"] == "apply"
    assert kw["result"]["error"].startswith("apply not started: the job's credentials expire in")


def test_apply_budget_is_cut_to_the_deadline(monkeypatch, tmp_path: Path) -> None:
    calls = _fake_tofu(monkeypatch)
    client = _Client()
    main.handle_apply(
        client,  # type: ignore[arg-type]
        _job("apply"),
        _settings(tmp_path),  # type: ignore[arg-type]
        deadline=Deadline(time.monotonic() + 660),
    )
    timeouts = dict(calls)
    assert 590 <= timeouts["apply"] <= 600  # not the 900 s budget: 660 - 60 s margin
    assert timeouts["init"] <= 600 and timeouts["output"] <= 600
    assert any("apply time budget cut from 900s" in line for line in client.lines)
    assert client.events[-1][0] == "phase_finished"


def test_an_apply_claim_anchors_the_deadline_on_its_tokens(monkeypatch, tmp_path: Path) -> None:
    """Through the agent's own path: the claim carries a state token living 200 s."""
    calls = _fake_tofu(monkeypatch)
    monkeypatch.setattr(main, "pick", lambda *a, **k: None)
    client = _Client()
    job = _job("apply", backend={"password": _jwt(200)})
    settings = SimpleNamespace(**vars(_settings(tmp_path)), available_tools=lambda: {})
    main._run_job(client, job, settings)  # type: ignore[arg-type]
    assert "apply" not in [c[0] for c in calls]
    assert client.events[-1][1]["result"]["error"].startswith("apply not started")


def test_plan_steps_are_cut_to_the_oidc_token(monkeypatch, tmp_path: Path) -> None:
    """A plan's OIDC token lives 30 min: no step gets the 2 h default."""
    calls = _fake_tofu(monkeypatch)
    client = _Client()
    job = _job("plan", cloud_credentials={"oidc_token": _jwt(1800), "role_arn": "arn:x"})
    main.handle_plan(client, job, _settings(tmp_path))  # type: ignore[arg-type]
    assert {c[0] for c in calls} >= {"init", "plan", "show"}
    assert all(t <= 1800 - 60 for _, t in calls), calls
    assert client.events[-1][0] == "phase_finished"


def test_a_plan_without_short_lived_credentials_keeps_its_budgets(monkeypatch, tmp_path) -> None:
    calls = _fake_tofu(monkeypatch)
    main.handle_plan(_Client(), _job("plan"), _settings(tmp_path))  # type: ignore[arg-type]
    assert dict(calls)["plan"] == _DEFAULT_TIMEOUT_SECONDS


def _command_job(name: str, cred_phase: str) -> dict:
    return {**_job("command", command={"name": name, "args": []}), "cred_phase": cred_phase}


def test_a_mutating_command_is_not_started_with_too_little_left(monkeypatch, tmp_path) -> None:
    calls = _fake_tofu(monkeypatch)
    client = _Client()
    main.handle_command_run(
        client,  # type: ignore[arg-type]
        _command_job("state rm", "apply"),
        _settings(tmp_path),  # type: ignore[arg-type]
        deadline=Deadline(time.monotonic() + 200),
    )
    assert [c[0] for c in calls] == ["init"]
    kind, kw = client.events[-1]
    assert kind == "job_failed" and "state rm not started" in kw["result"]["error"]


def test_a_read_only_command_is_only_cut(monkeypatch, tmp_path: Path) -> None:
    calls = _fake_tofu(monkeypatch)
    client = _Client()
    main.handle_command_run(
        client,  # type: ignore[arg-type]
        _command_job("state list", "plan"),
        _settings(tmp_path),  # type: ignore[arg-type]
        deadline=Deadline(time.monotonic() + 200),
    )
    assert 130 <= dict(calls)["state"] <= 140
    assert client.events[-1][0] == "phase_finished"
