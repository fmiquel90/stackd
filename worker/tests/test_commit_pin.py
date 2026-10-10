"""Commit pinning (SPECS §7.2/§7.3): a plan job clones its environment's branch head (or the pinned
commit) and reports the exact SHA; an apply job clones exactly the pinned SHA, never a branch head.
The git tests use a real local repository over file:// (the shallow-clone transport)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent.main as main
from agent.workspace import Workspace


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, str]:
    """main: c1 → c2 → c3 (tip); staging forks at c2 → s1. Returns the URL and the SHAs."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    shas: dict[str, str] = {}
    for name in ("c1", "c2"):
        (src / "main.tf").write_text(f"# {name}\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", name)
        shas[name] = _git(src, "rev-parse", "HEAD")
    _git(src, "checkout", "-q", "-b", "staging")
    (src / "main.tf").write_text("# s1\n")
    _git(src, "commit", "-qam", "s1")
    shas["s1"] = _git(src, "rev-parse", "HEAD")
    _git(src, "checkout", "-q", "main")
    (src / "main.tf").write_text("# c3\n")
    _git(src, "commit", "-qam", "c3")
    shas["c3"] = _git(src, "rev-parse", "HEAD")
    return {"url": f"file://{src}", **shas}


def _clone(tmp_path: Path, repo: dict[str, str], commit: str | None, **kw: Any) -> Workspace:
    ws = Workspace(str(tmp_path / "ws"), f"job-{commit}-{kw.get('branch')}")
    ws.git_clone(repo["url"], commit, ".", **kw)
    return ws


def test_unpinned_clone_takes_the_environment_branch_head(tmp_path: Path, repo: dict) -> None:
    # Not the remote's default branch: the head of the branch the environment tracks.
    ws = _clone(tmp_path, repo, None, branch="staging")
    assert ws.head_sha() == repo["s1"]


@pytest.mark.parametrize("commit", ["c1", "s1"])
def test_pinned_sha_off_the_default_tip_is_checked_out_exactly(
    tmp_path: Path, repo: dict, commit: str
) -> None:
    # An older commit of main, and the tip of another branch: neither is in the shallow clone.
    ws = _clone(tmp_path, repo, repo[commit], branch="main")
    assert ws.head_sha() == repo[commit]


def test_pinned_ref_checks_out_what_it_resolves_to(tmp_path: Path, repo: dict) -> None:
    ws = _clone(tmp_path, repo, "staging")
    assert ws.head_sha() == repo["s1"]


def test_unknown_pinned_commit_fails_at_fetch(tmp_path: Path, repo: dict) -> None:
    with pytest.raises(RuntimeError, match=r"^git fetch 0{40} failed: "):
        _clone(tmp_path, repo, "0" * 40, branch="main")


# --- handlers ----------------------------------------------------------------------------------


class _FakeClient:
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


class _FakeWorkspace:
    clones: list[tuple[Any, ...]] = []

    def __init__(self, root: str, job_id: str) -> None:
        self.root = Path(root)

    def git_clone(self, repo_url: str, commit_sha: str | None, *args: Any, **kw: Any) -> Path:
        self.clones.append((commit_sha, kw.get("branch")))
        return self.root

    def head_sha(self) -> str:
        return "c" * 40

    def write_tfvars(self, *args: Any) -> None:
        pass

    def write_hcl_tfvars(self, *args: Any) -> None:
        pass

    def write_backend_override(self, *args: Any) -> None:
        pass

    def write_plan(self, cwd: Path, data: bytes) -> Path:
        return cwd / "plan.tfplan"

    def load_stackd_yml(self, cwd: Path) -> dict:
        return {}

    def cleanup(self) -> None:
        pass


def _handle(monkeypatch, tmp_path: Path, handler: str, commit_sha: str | None) -> _FakeClient:
    monkeypatch.setattr(main, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(main, "run_command", lambda *a, **kw: 0)
    monkeypatch.setattr(main, "stream_json_command", lambda *a, **kw: (2, []))
    monkeypatch.setattr(
        main.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "{}", "")
    )
    _FakeWorkspace.clones = []
    client = _FakeClient()
    job = {
        "job_id": "run-1",
        "phase": "plan" if handler == "handle_plan" else "apply",
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": commit_sha,
            "branch": "staging",
            "project_root": ".",
        },
    }
    getattr(main, handler)(client, job, SimpleNamespace(workspace_root=str(tmp_path)))
    return client


def test_plan_reports_the_commit_it_checked_out(monkeypatch, tmp_path: Path) -> None:
    client = _handle(monkeypatch, tmp_path, "handle_plan", None)
    assert _FakeWorkspace.clones == [(None, "staging")]
    started = [kw for kind, kw in client.events if kind == "phase_started"]
    assert started[0] == {"phase": "planning", "result": {"commit_sha": "c" * 40}}


def test_apply_without_a_pinned_commit_fails_closed(monkeypatch, tmp_path: Path) -> None:
    # Never fall back to the branch head: it may have moved since the reviewed plan.
    client = _handle(monkeypatch, tmp_path, "handle_apply", None)
    assert _FakeWorkspace.clones == []
    [(kind, kw)] = client.events
    assert kind == "job_failed"
    assert "no pinned commit" in kw["result"]["error"]
