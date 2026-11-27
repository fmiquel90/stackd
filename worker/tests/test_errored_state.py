"""errored.tfstate (SPECS §7.3): the state tofu could not write back — an expired token, the backend
unreachable — is the only record of what the apply changed. It is uploaded as the run's artifact
before the workspace is removed, or kept on the worker when the API does not take it; its content
never reaches a log."""

from __future__ import annotations

import os
import stat
import time
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

import agent.main as main
from agent.deadline import Deadline

STATE = b'{"version": 4, "serial": 8, "resources": [{"password": "s3cr3t-in-state"}]}'


class _Client:
    def __init__(self, upload_error: Exception | None = None) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lines: list[str] = []
        self.uploads: dict[str, bytes] = {}
        self.upload_error = upload_error

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.lines.extend(line["msg"] for line in lines)

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        if name == "errored.tfstate" and self.upload_error is not None:
            raise self.upload_error
        self.uploads[name] = data

    def download_artifact(self, job_id: str, name: str) -> bytes:
        return b"PK saved plan"


class _Workspace:
    """A plain directory as the checkout; cleanup really removes it, as the agent's does."""

    def __init__(self, root: str, job_id: str) -> None:
        self.path = Path(root) / job_id
        (self.path / "repo").mkdir(parents=True, exist_ok=True)

    def git_clone(self, *args: object, **kw: object) -> Path:
        return self.path / "repo"

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

    def cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.path, ignore_errors=True)


def _job(phase: str = "apply", **extra: Any) -> dict:
    return {
        "job_id": "run-1",
        "phase": phase,
        "cred_phase": "apply",
        "apply_timeout_seconds": 900,
        "mask_values": ["s3cr3t-in-state"],
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "project_root": ".",
        },
        **extra,
    }


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        runner="local", workspace_root=str(tmp_path), leak_action="warn", worker_name="w1"
    )


def _fake_tofu(monkeypatch, *, writes_errored: bool, rc: int = 1) -> None:
    """`apply` (or the command) exits `rc`, leaving errored.tfstate behind when asked to."""

    def run_tofu(cmd: list[str], cwd: Path, *a: Any, **kw: Any) -> Any:
        if cmd[1] in ("apply", "state") and writes_errored:
            (cwd / "errored.tfstate").write_bytes(STATE)
        code = rc if cmd[1] in ("apply", "state") else 0
        return (code, []) if cmd[1] == "apply" else code

    monkeypatch.setattr(main, "Workspace", _Workspace)
    monkeypatch.setattr(main, "run_command", run_tofu)
    monkeypatch.setattr(main, "stream_json_command", run_tofu)
    monkeypatch.setattr(
        type(main.LOCAL),
        "capture",
        lambda self, cmd, *a, **kw: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )


def _apply(client: _Client, tmp_path: Path) -> None:
    main.handle_apply(
        client,  # type: ignore[arg-type]
        _job(),
        _settings(tmp_path),  # type: ignore[arg-type]
        deadline=Deadline(None),
    )


def test_errored_state_is_uploaded_before_the_failure_and_the_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    _fake_tofu(monkeypatch, writes_errored=True)
    client = _Client()
    _apply(client, tmp_path)
    assert client.uploads["errored.tfstate"] == STATE  # byte for byte, never masked
    assert client.events[-1][0] == "job_failed"
    assert not (tmp_path / "run-1").exists()  # the workspace is gone as before
    saved = [line for line in client.lines if "errored.tfstate" in line]
    assert saved and "saved with the run" in saved[0]
    assert all("s3cr3t-in-state" not in line and "serial" not in line for line in client.lines)


def test_errored_state_is_kept_on_the_worker_when_the_api_refuses_it(
    monkeypatch, tmp_path: Path
) -> None:
    refused = httpx.HTTPStatusError(
        "409 Conflict",
        request=httpx.Request("PUT", "http://api/x"),
        response=httpx.Response(409),
    )
    _fake_tofu(monkeypatch, writes_errored=True)
    client = _Client(upload_error=refused)
    _apply(client, tmp_path)
    kept = list((tmp_path / ".errored-state").iterdir())
    assert len(kept) == 1 and kept[0].read_bytes() == STATE
    assert stat.S_IMODE(kept[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".errored-state").stat().st_mode) == 0o700
    line = next(line for line in client.lines if "errored.tfstate" in line)
    assert "not uploaded" in line and str(kept[0]) in line and "w1" in line


def test_a_successful_apply_uploads_nothing(monkeypatch, tmp_path: Path) -> None:
    _fake_tofu(monkeypatch, writes_errored=False, rc=0)
    client = _Client()
    _apply(client, tmp_path)
    assert "errored.tfstate" not in client.uploads
    assert client.events[-1][0] == "phase_finished"


def test_an_errored_state_the_apply_did_not_write_is_not_its(monkeypatch, tmp_path: Path) -> None:
    """One committed in the repo, or a symlink a hook planted: never read nor uploaded."""
    _fake_tofu(monkeypatch, writes_errored=False)
    repo = tmp_path / "run-1" / "repo"
    repo.mkdir(parents=True)
    (repo / "errored.tfstate").write_bytes(b"committed by mistake")
    client = _Client()
    _apply(client, tmp_path)
    assert "errored.tfstate" not in client.uploads

    secret = tmp_path / "host-secret"
    secret.write_text("not yours")

    def apply_plants_a_symlink(cmd: list[str], cwd: Path, *a: Any, **kw: Any) -> Any:
        if cmd[1] == "apply":
            os.symlink(secret, cwd / "errored.tfstate")
            return 1, []
        return 0

    monkeypatch.setattr(main, "stream_json_command", apply_plants_a_symlink)
    _apply(client, tmp_path)
    assert "errored.tfstate" not in client.uploads


def test_a_fifo_does_not_hang_the_job(monkeypatch, tmp_path: Path) -> None:
    _fake_tofu(monkeypatch, writes_errored=False)

    def apply_plants_a_fifo(cmd: list[str], cwd: Path, *a: Any, **kw: Any) -> Any:
        if cmd[1] == "apply":
            os.mkfifo(cwd / "errored.tfstate")
            return 1, []
        return 0

    monkeypatch.setattr(main, "stream_json_command", apply_plants_a_fifo)
    client = _Client()
    started = time.monotonic()
    _apply(client, tmp_path)
    assert time.monotonic() - started < 5
    assert "errored.tfstate" not in client.uploads and client.events[-1][0] == "job_failed"


def test_a_mutating_command_preserves_its_errored_state(monkeypatch, tmp_path: Path) -> None:
    _fake_tofu(monkeypatch, writes_errored=True)
    client = _Client()
    job = _job("command", command={"name": "state rm", "args": ["aws_s3_bucket.x"]})
    main.handle_command_run(client, job, _settings(tmp_path), deadline=Deadline(None))  # type: ignore[arg-type]
    assert client.uploads["errored.tfstate"] == STATE
    assert client.events[-1][0] == "job_failed"
