from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.main as main
from agent.runner import _DEFAULT_TIMEOUT_SECONDS


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, job_id: str, kind: str, **kw: object) -> None:
        self.events.append((kind, kw))

    def logs(self, *args: object, **kw: object) -> None:
        pass


class _FakeWorkspace:
    def __init__(self, root: str, job_id: str) -> None:
        self.root = Path(root)

    def git_clone(self, *args: object, **kw: object) -> Path:
        return self.root

    def write_tfvars(self, *args: object) -> None:
        pass

    def write_hcl_tfvars(self, *args: object) -> None:
        pass

    def write_backend_override(self, *args: object) -> None:
        pass

    def cleanup(self) -> None:
        pass


def _job(name: str) -> dict:
    return {
        "job_id": "run-1",
        "phase": "command",
        "apply_timeout_seconds": 900,
        "command": {"name": name, "args": []},
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": None,
            "project_root": ".",
        },
    }


def _run(monkeypatch, tmp_path: Path, name: str, rc: int) -> tuple[_FakeClient, list[int]]:
    """Run handle_command_run with the clone and subprocesses faked; init succeeds, the subcommand
    exits `rc`. Returns the client (reported events) and the timeout each subprocess got."""
    timeouts: list[int] = []

    def fake_run_command(cmd: list[str], *args: object, **kw: int) -> int:
        timeouts.append(kw.get("timeout", _DEFAULT_TIMEOUT_SECONDS))
        return 0 if cmd[1] == "init" else rc

    monkeypatch.setattr(main, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(main, "run_command", fake_run_command)
    client = _FakeClient()
    main.handle_command_run(
        client,
        _job(name),
        SimpleNamespace(workspace_root=str(tmp_path)),
    )
    return client, timeouts


@pytest.mark.parametrize("name", ["state rm", "import", "state mv", "taint", "refresh"])
def test_mutating_command_gets_apply_budget(monkeypatch, tmp_path: Path, name: str) -> None:
    # A mutating command holds apply credentials → the apply budget (§4.2/§4.3), not the 2h default.
    _client, timeouts = _run(monkeypatch, tmp_path, name, rc=0)
    assert timeouts[-1] == 900


@pytest.mark.parametrize("name", ["state list", "output", "show", "validate"])
def test_read_only_command_keeps_default_timeout(monkeypatch, tmp_path: Path, name: str) -> None:
    _client, timeouts = _run(monkeypatch, tmp_path, name, rc=0)
    assert timeouts[-1] == _DEFAULT_TIMEOUT_SECONDS


def test_unknown_command_fails_closed_to_apply_budget(monkeypatch, tmp_path: Path) -> None:
    _client, timeouts = _run(monkeypatch, tmp_path, "some future command", rc=0)
    assert timeouts[-1] == 900


def test_command_over_budget_fails_with_clear_message(monkeypatch, tmp_path: Path) -> None:
    client, _ = _run(monkeypatch, tmp_path, "state rm", rc=124)  # 124 = watchdog kill
    kind, kw = client.events[-1]
    assert kind == "job_failed"
    assert kw["result"] == {"error": "state rm exceeded the 900s time budget and was terminated"}
