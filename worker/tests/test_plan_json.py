"""The plan job's plan JSON (`tofu show -json`, SPECS §8.3): parsed once for every reader — the
forget count, the cleartext tripwire (§5.1), the masked copy — and each still sees what it needs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent.main as main
from agent.workspace import Workspace

SECRET = "pw-0f-the-db"
PLAN = {
    "planned_values": {
        "outputs": {"echo": {"sensitive": False, "value": SECRET}},
        "root_module": {},
    },
    "resource_changes": [{"address": "a.x", "change": {"actions": ["forget"]}}],
}


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


class _Workspace(Workspace):
    def git_clone(self, *args: Any, **kw: Any) -> Path:
        (self.path / "repo").mkdir(exist_ok=True)
        return self.path / "repo"

    def head_sha(self) -> str:
        return "c" * 40


def test_the_plan_json_is_parsed_once_and_each_reader_gets_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = json.dumps(PLAN)

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple:
        out = next(a.removeprefix("-out=") for a in cmd if a.startswith("-out="))
        Path(out).write_bytes(b"PK-plan")
        return 2, []

    monkeypatch.setattr(main, "Workspace", _Workspace)
    monkeypatch.setattr(main, "run_command", lambda *a, **kw: 0)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(
        main.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, doc, "")
    )
    parses = 0
    real_loads = json.loads

    def counting_loads(s: Any, *args: Any, **kw: Any) -> Any:
        nonlocal parses
        parses += s == doc
        return real_loads(s, *args, **kw)

    monkeypatch.setattr(json, "loads", counting_loads)
    client = _Client()
    job = {
        "job_id": "run-1",
        "phase": "plan",
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": None,
            "branch": "main",
            "project_root": ".",
        },
        "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "mask_values": [SECRET],
    }
    settings = SimpleNamespace(runner="local", workspace_root=str(tmp_path), leak_action="warn")
    main.handle_plan(client, job, settings)  # type: ignore[arg-type]

    assert parses == 1
    kind, finished = client.events[-1]
    assert kind == "phase_finished"
    result = finished["result"]
    assert result["summary"]["forget"] == 1
    # The tripwire read the clear values, before the masking redacted them in place.
    [leak] = [c for c in result["checks"] if c["name"] == "secret_leak_suspected"]
    assert "echo" in leak["detail"]
    masked = json.loads(client.uploads["plan.json"])
    assert masked["planned_values"]["outputs"]["echo"]["value"] == "***"
