"""Graceful shutdown (agent.shutdown): on SIGTERM the agent stops claiming, lets its running jobs
finish within STACKD_SHUTDOWN_TIMEOUT_SECONDS, then reports the ones still running failed and kills
their commands. A second signal aborts at once."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

import agent.main as main
from agent import shutdown
from agent.client import ApiClient
from agent.config import Settings
from agent.runner import LocalRunner


@pytest.fixture(autouse=True)
def fresh_shutdown(monkeypatch: pytest.MonkeyPatch) -> shutdown.Shutdown:
    stop = shutdown.Shutdown()
    monkeypatch.setattr(shutdown, "SHUTDOWN", stop)
    return stop


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "api_url": "http://api",
        "pool_token": "pool",
        "worker_name": "w",
        "runner": "local",
        "heartbeat_interval": 1,
        "poll_wait": 0,
        "workspace_root": "/tmp/stackd-ws-test",
        "leak_action": "warn",
        "max_concurrent_jobs": 1,
    }
    base.update(kw)
    return Settings(**base)


class _Client:
    """Hands out `jobs` one claim at a time, then nothing; records the events reported."""

    def __init__(self, jobs: list[dict]) -> None:
        self._jobs = list(jobs)
        self.events: list[tuple[str, str, dict]] = []
        self.claims = 0

    def claim(self, wait: int) -> dict | None:
        self.claims += 1
        if self._jobs:
            return self._jobs.pop(0)
        time.sleep(0.01)
        return None

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((job_id, kind, kw))


def _serve_in_thread(client: _Client, settings: Settings) -> threading.Thread:
    t = threading.Thread(
        target=main._serve, args=(cast(ApiClient, client), settings, main._InFlight()), daemon=True
    )
    t.start()
    return t


def test_requests_then_abort_kill_registered_commands(
    fresh_shutdown: shutdown.Shutdown,
) -> None:
    killed: list[str] = []
    with fresh_shutdown.command(lambda: killed.append("a")):
        fresh_shutdown.request()
        assert fresh_shutdown.requested and not fresh_shutdown.forced
        fresh_shutdown.request()
        assert fresh_shutdown.forced
        assert killed == []  # the drain reports the jobs first, then aborts
        fresh_shutdown.abort()
        assert fresh_shutdown.aborting
        assert killed == ["a"]
    # Once aborting, a command a job would still start is killed at once.
    with fresh_shutdown.command(lambda: killed.append("late")):
        pass
    assert killed == ["a", "late"]


def test_sigterm_requests_a_drain(fresh_shutdown: shutdown.Shutdown) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        fresh_shutdown.install()
        os.kill(os.getpid(), signal.SIGTERM)
        assert fresh_shutdown.wait(2)
        assert not fresh_shutdown.forced
    finally:
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, signal.default_int_handler)


def test_abort_kills_a_running_local_command(
    fresh_shutdown: shutdown.Shutdown, tmp_path: Path
) -> None:
    result: dict[str, Any] = {}

    def _run() -> None:
        result["rc"] = LocalRunner().stream(
            ["sleep", "30"], tmp_path, dict(os.environ), timeout=60, on_line=lambda _l: None
        )

    t = threading.Thread(target=_run)
    started = time.monotonic()
    t.start()
    time.sleep(0.3)
    fresh_shutdown.abort()
    t.join(10)
    assert not t.is_alive()
    rc, timed_out = result["rc"]
    assert rc == -signal.SIGKILL
    assert timed_out is False  # an abort is not reported as a time-budget kill
    assert time.monotonic() - started < 10


def test_drain_lets_the_running_job_finish(
    monkeypatch: pytest.MonkeyPatch, fresh_shutdown: shutdown.Shutdown
) -> None:
    release = threading.Event()
    running = threading.Event()
    finished: list[str] = []

    def fake_run_job(client: Any, job: dict, settings: Settings, checks: Any = None) -> None:
        running.set()
        release.wait(10)
        finished.append(job["job_id"])

    monkeypatch.setattr(main, "_run_job", fake_run_job)
    client = _Client([{"job_id": "j1", "phase": "apply"}])
    t = _serve_in_thread(client, _settings(shutdown_timeout=30))
    assert running.wait(5)

    fresh_shutdown.request()
    time.sleep(0.3)
    claims_at_stop = client.claims
    time.sleep(0.5)
    assert client.claims == claims_at_stop  # no claim once the stop is requested
    assert t.is_alive()  # still draining: the job runs on

    release.set()
    t.join(5)
    assert not t.is_alive()
    assert finished == ["j1"]
    assert client.events == []  # the job ended on its own: nothing reported in its place


def test_drain_budget_exceeded_reports_then_kills(
    monkeypatch: pytest.MonkeyPatch, fresh_shutdown: shutdown.Shutdown, tmp_path: Path
) -> None:
    outcome: dict[str, Any] = {}

    def fake_run_job(client: Any, job: dict, settings: Settings, checks: Any = None) -> None:
        # A job stuck in a long command: only the abort ends it.
        outcome["rc"] = LocalRunner().stream(
            ["sleep", "60"], tmp_path, dict(os.environ), timeout=120, on_line=lambda _l: None
        )

    monkeypatch.setattr(main, "_run_job", fake_run_job)
    client = _Client([{"job_id": "j1", "phase": "apply"}])
    t = _serve_in_thread(client, _settings(shutdown_timeout=1))
    time.sleep(0.5)
    fresh_shutdown.request()
    t.join(15)
    assert not t.is_alive()
    assert fresh_shutdown.aborting
    assert outcome["rc"][0] == -signal.SIGKILL
    [(job_id, kind, kw)] = client.events
    assert (job_id, kind, kw["phase"]) == ("j1", "job_failed", "apply")
    assert "drain budget of 1s exceeded" in kw["result"]["error"]


def test_second_signal_aborts_without_waiting_for_the_budget(
    monkeypatch: pytest.MonkeyPatch, fresh_shutdown: shutdown.Shutdown, tmp_path: Path
) -> None:
    def fake_run_job(client: Any, job: dict, settings: Settings, checks: Any = None) -> None:
        LocalRunner().stream(
            ["sleep", "60"], tmp_path, dict(os.environ), timeout=120, on_line=lambda _l: None
        )

    monkeypatch.setattr(main, "_run_job", fake_run_job)
    client = _Client([{"job_id": "j1", "phase": "plan"}])
    t = _serve_in_thread(client, _settings(shutdown_timeout=3600))
    time.sleep(0.5)
    fresh_shutdown.request()
    time.sleep(0.3)
    fresh_shutdown.request()
    t.join(15)
    assert not t.is_alive()
    [(_job_id, kind, kw)] = client.events
    assert kind == "job_failed"
    assert "forced stop" in kw["result"]["error"]


def test_shutdown_timeout_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STACKD_SHUTDOWN_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().shutdown_timeout == 1800
    monkeypatch.setenv("STACKD_SHUTDOWN_TIMEOUT_SECONDS", "120")
    assert Settings.from_env().shutdown_timeout == 120
