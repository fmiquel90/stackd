"""The agent's git (SPECS §7.4): a stalled Git host can't hold a job — each git command has a time
budget (`STACKD_GIT_TIMEOUT_SECONDS`), and a stop request (§4.1) interrupts it. A stand-in `ssh`
that never answers plays the stalled host."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from agent import cancel
from agent.workspace import Workspace

_STALLED_SSH = "#!/bin/sh\nsleep 60\n"


@pytest.fixture
def stalled_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(_STALLED_SSH)
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return "git@stalled.example.com:org/infra.git"


@pytest.mark.parametrize("commit", [None, "a" * 40])
def test_a_stalled_git_host_fails_the_clone_at_its_timeout(
    tmp_path: Path, stalled_host: str, monkeypatch: pytest.MonkeyPatch, commit: str | None
) -> None:
    monkeypatch.setenv("STACKD_GIT_TIMEOUT_SECONDS", "1")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"failed: timed out after 1s$"):
        Workspace(str(tmp_path / "ws"), "job").git_clone(stalled_host, commit, ".")
    assert time.monotonic() - started < 15


@pytest.mark.parametrize("commit", [None, "a" * 40])
def test_a_stop_request_interrupts_the_clone(
    tmp_path: Path, stalled_host: str, commit: str | None
) -> None:
    token = cancel.CancelToken(grace=1)
    cancel.bind(token)
    stop = threading.Timer(0.5, token.request)
    stop.start()
    started = time.monotonic()
    try:
        with pytest.raises(cancel.JobCanceled):
            Workspace(str(tmp_path / "ws"), "job").git_clone(stalled_host, commit, ".")
    finally:
        stop.cancel()
        cancel.bind(None)
    assert token.interrupted and time.monotonic() - started < 15


def test_a_job_stopped_before_its_clone_runs_no_git(tmp_path: Path, stalled_host: str) -> None:
    token = cancel.CancelToken()
    token.request()
    cancel.bind(token)
    try:
        with pytest.raises(cancel.JobCanceled):
            Workspace(str(tmp_path / "ws"), "job").git_clone(stalled_host, None, ".")
    finally:
        cancel.bind(None)
    assert not token.interrupted  # nothing was running to interrupt
