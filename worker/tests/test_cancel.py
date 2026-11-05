"""Stopping a job on request (SPECS §4.1, §7.1): SIGINT, then SIGKILL after the grace period."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent import cancel
from agent.config import Settings
from agent.main import _handle_command, _run_job
from agent.runner import _stream_proc

GRACEFUL = (
    "import signal, sys, time\n"
    "def stop(*_):\n"
    "    print('graceful stop', flush=True); sys.exit(3)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)
STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


class Streamer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        self.lines.extend(lines)


def _run_in_thread(
    script: str, token: cancel.CancelToken, streamer: Streamer, tmp_path: Path
) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def target() -> None:
        cancel.bind(token)
        try:
            out["rc"] = _stream_proc(
                [sys.executable, "-c", script],
                tmp_path,
                {},
                phase="applying",
                section=None,
                streamer=streamer,  # type: ignore[arg-type]
                timeout=60,
                transform=lambda line: line,
            )
        except cancel.JobCanceled:
            out["canceled"] = True

    thread = threading.Thread(target=target)
    thread.start()
    return thread, out


def _wait_ready(thread: threading.Thread) -> None:
    # The child installs its signal handler right after starting: give it a moment (the `ready`
    # line only reaches the streamer when the stream ends).
    time.sleep(1.0)
    assert thread.is_alive()


def test_sigint_stops_the_tool_gracefully(tmp_path: Path) -> None:
    token, streamer = cancel.CancelToken(grace=30), Streamer()
    thread, out = _run_in_thread(GRACEFUL, token, streamer, tmp_path)
    _wait_ready(thread)
    token.request()
    thread.join(10)
    assert not thread.is_alive()
    assert out.get("canceled") is True  # the job is stopped, not "failed with exit 3"
    assert "graceful stop" in streamer.lines  # the tool handled SIGINT and exited by itself
    assert "[stackd] stopped on request" in streamer.lines


def test_a_tool_ignoring_sigint_is_killed_after_the_grace_period(tmp_path: Path) -> None:
    token, streamer = cancel.CancelToken(grace=1), Streamer()
    thread, out = _run_in_thread(STUBBORN, token, streamer, tmp_path)
    _wait_ready(thread)
    started = time.monotonic()
    token.request()
    thread.join(15)
    assert not thread.is_alive()
    assert out.get("canceled") is True
    assert 0.8 < time.monotonic() - started < 10  # not before the grace, well before the 60 s sleep


def test_a_stop_asked_between_two_commands_starts_no_next_one(tmp_path: Path) -> None:
    token, streamer = cancel.CancelToken(), Streamer()
    token.request()  # nothing runs: nothing to signal yet
    thread, out = _run_in_thread(GRACEFUL, token, streamer, tmp_path)
    thread.join(5)
    assert out.get("canceled") is True
    assert streamer.lines == []  # the command never started


def test_after_the_point_of_no_return_a_stop_no_longer_applies(tmp_path: Path) -> None:
    token, streamer = cancel.CancelToken(), Streamer()
    token.disarm()  # the apply succeeded: the remaining bookkeeping must finish
    token.request()
    thread, out = _run_in_thread("print('bookkeeping')", token, streamer, tmp_path)
    thread.join(10)
    assert out == {"rc": 0}
    assert "bookkeeping" in streamer.lines


COMPLETES = (
    "import signal, sys, time\n"
    "def done(*_):\n"
    "    print('finished anyway', flush=True); sys.exit(0)\n"
    "signal.signal(signal.SIGINT, done)\n"
    "time.sleep(60)\n"
)


def test_a_command_that_completes_despite_the_signal_is_not_a_stop(tmp_path: Path) -> None:
    """The stop raced the end of the apply: exit 0 means it went through — never reported as
    interrupted (it would read as a partial apply); the next command is not started, unless the
    job was disarmed first."""
    token, streamer = cancel.CancelToken(grace=30), Streamer()
    thread, out = _run_in_thread(COMPLETES, token, streamer, tmp_path)
    _wait_ready(thread)
    token.request()
    thread.join(10)
    assert out == {"rc": 0}
    assert "[stackd] stopped on request" not in streamer.lines

    # The apply succeeded → disarmed: a later failing command is a failure, not a stop.
    token.disarm()
    thread, out = _run_in_thread("import sys; sys.exit(1)", token, streamer, tmp_path)
    thread.join(10)
    assert out == {"rc": 1}


def test_registry_finds_the_job_and_remembers_an_early_cancel() -> None:
    jobs = cancel.JobRegistry(grace=5)
    token = jobs.register("run-1")
    assert not token.requested
    assert jobs.cancel("run-1") is True
    assert token.requested
    jobs.unregister("run-1")
    # The heartbeat beat the claim: the cancel is applied when the job registers.
    assert jobs.cancel("run-2") is False
    assert jobs.register("run-2").requested
    assert not jobs.register("run-3").requested


class FakeClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict | None]] = []
        self.results: list[tuple[str, dict, str]] = []

    def event(self, job_id: str, name: str, phase: str | None = None, result: dict | None = None):  # type: ignore[no-untyped-def]
        self.events.append((name, phase, result))

    def command_result(self, command_id: str, result: dict, status: str = "done") -> None:
        self.results.append((command_id, result, status))


def test_cancel_job_command_reaches_the_running_job() -> None:
    jobs, client = cancel.JobRegistry(), FakeClient()
    token = jobs.register("run-9")
    _handle_command(
        client,  # type: ignore[arg-type]
        {"id": "c1", "type": "cancel_job", "payload": {"job_id": "run-9"}},
        Settings.from_env(),
        jobs,
    )
    assert token.requested
    assert client.results == [("c1", {"job_id": "run-9", "running_here": True}, "done")]


def test_a_canceled_job_is_reported_as_job_canceled_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.main as main

    def stopped(*_a: object, **_k: object) -> None:
        raise cancel.JobCanceled

    monkeypatch.setattr(main, "pick", lambda *a, **k: None)
    monkeypatch.setattr(main, "handle_plan", stopped)
    monkeypatch.setattr(Settings, "available_tools", lambda self: {})
    client = FakeClient()
    job = {
        "job_id": "run-5",
        "phase": "plan",
        "environment": {"tool": "opentofu", "tool_version": "1.12.0"},
    }
    _run_job(client, job, Settings.from_env())  # type: ignore[arg-type]
    assert [e[0] for e in client.events] == ["job_canceled"]
    assert client.events[0][1] == "plan"
