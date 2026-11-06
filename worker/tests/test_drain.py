"""Graceful drain on SIGTERM/SIGINT (SPECS §7.1 "Drain"): no new claim, the running jobs finish and
report, then exit 0; past the drain timeout or on a second signal, the cancel path stops them."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

import agent.drain as drain_mod
import agent.main as main
from agent import cancel
from agent.config import Settings
from agent.drain import Drain, supervise

JOB = {
    "job_id": "run-1",
    "phase": "plan",
    "environment": {"tool": "opentofu", "tool_version": "1.12.2"},
}


# --- the state machine ----------------------------------------------------------------------


def test_first_signal_drains_second_forces() -> None:
    d = Drain(timeout=10)
    assert not d.draining and not d.forced
    d.on_signal(signal.SIGTERM)
    assert d.draining and not d.forced
    d.on_signal(signal.SIGINT)
    assert d.draining and d.forced
    assert d.signal == signal.SIGINT


def test_drain_sleep_returns_early_once_draining() -> None:
    d = Drain()
    threading.Timer(0.2, d.on_signal, args=(signal.SIGTERM,)).start()
    started = time.monotonic()
    d.sleep(30)
    assert time.monotonic() - started < 5


class Counter:
    def __init__(self, n: int) -> None:
        self.n = n
        self.stops = 0

    def count(self) -> int:
        return self.n

    def stop_all(self) -> int:
        self.stops += 1
        return self.n


def _supervise_in_thread(d: Drain, jobs: Counter, grace: float = 5) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def target() -> None:
        out["outcome"] = supervise(
            d, in_flight=jobs.count, stop_all=jobs.stop_all, cancel_grace=grace, poll=0.02
        )
        out["at"] = time.monotonic()

    out["thread"] = threading.Thread(target=target)
    out["thread"].start()
    return out


def test_in_flight_jobs_finish_then_drained() -> None:
    d, jobs = Drain(timeout=30), Counter(1)
    out = _supervise_in_thread(d, jobs)
    d.on_signal(signal.SIGTERM)
    d.claims_stopped = True
    time.sleep(0.3)
    assert out["thread"].is_alive()  # the job is still running: no exit
    jobs.n = 0  # it finished (and reported: `_execute` frees the count after `_run_job`)
    out["thread"].join(5)
    assert out["outcome"] == "drained"
    assert jobs.stops == 0  # never canceled


def test_drain_waits_for_the_claim_in_flight() -> None:
    """Idle is not enough while a long-poll may still hand over a job."""
    d, jobs = Drain(timeout=30), Counter(0)
    out = _supervise_in_thread(d, jobs)
    d.on_signal(signal.SIGTERM)
    time.sleep(0.3)
    assert out["thread"].is_alive()
    d.claims_stopped = True
    out["thread"].join(5)
    assert out["outcome"] == "drained"


def test_timeout_falls_back_to_the_cancel_path() -> None:
    d, jobs = Drain(timeout=0.3), Counter(2)
    d.claims_stopped = True
    original = jobs.stop_all

    def stop_all() -> int:  # the cancel path ends the jobs, which then report
        n = original()
        jobs.n = 0
        return n

    jobs.stop_all = stop_all  # type: ignore[method-assign]
    started = time.monotonic()
    out = _supervise_in_thread(d, jobs)
    d.on_signal(signal.SIGTERM)
    out["thread"].join(5)
    assert out["outcome"] == "stopped"
    assert jobs.stops == 1
    assert out["at"] - started >= 0.3  # not before the timeout


def test_second_signal_stops_at_once() -> None:
    d, jobs = Drain(timeout=3600), Counter(1)
    d.claims_stopped = True
    out = _supervise_in_thread(d, jobs)
    d.on_signal(signal.SIGTERM)
    time.sleep(0.2)
    assert jobs.stops == 0
    second = time.monotonic()
    d.on_signal(signal.SIGTERM)
    deadline = time.monotonic() + 5
    while jobs.stops == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert jobs.stops == 1
    assert time.monotonic() - second < 2  # immediately, not after the hour
    jobs.n = 0
    out["thread"].join(5)
    assert out["outcome"] == "stopped"


def test_a_job_that_never_ends_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drain_mod, "REPORT_MARGIN_SECONDS", 0.2)
    d, jobs = Drain(timeout=0), Counter(1)
    d.claims_stopped = True
    out = _supervise_in_thread(d, jobs, grace=0)
    d.on_signal(signal.SIGTERM)
    out["thread"].join(5)
    assert out["outcome"] == "abandoned"


def test_drain_timeout_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STACKD_DRAIN_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().drain_timeout_seconds == 1200
    monkeypatch.setenv("STACKD_DRAIN_TIMEOUT_SECONDS", "60")
    assert Settings.from_env().drain_timeout_seconds == 60


# --- the claim loop ---------------------------------------------------------------------------


class ClaimClient:
    def __init__(self, d: Drain, jobs: list[dict | None], signal_during: int | None = None) -> None:
        self.drain, self.jobs, self.signal_during = d, jobs, signal_during
        self.claims = 0

    def claim(self, wait: int) -> dict | None:
        self.claims += 1
        if self.claims == self.signal_during:
            self.drain.on_signal(signal.SIGTERM)  # the signal lands during the long-poll
        return self.jobs.pop(0) if self.jobs else None


def test_no_claim_once_draining() -> None:
    d = Drain()
    d.on_signal(signal.SIGTERM)
    client, started = ClaimClient(d, [JOB]), []
    main._claim_loop(client, Settings.from_env(), d, threading.Semaphore(1), started.append)  # type: ignore[arg-type]
    assert client.claims == 0 and started == []


def test_a_job_claimed_during_the_signal_still_runs() -> None:
    d = Drain()
    client, started = ClaimClient(d, [None, JOB], signal_during=2), []
    main._claim_loop(client, Settings.from_env(), d, threading.Semaphore(2), started.append)  # type: ignore[arg-type]
    assert started == [JOB]  # claimed: never dropped
    assert client.claims == 2  # and no claim after it


def test_waiting_for_a_slot_notices_the_drain() -> None:
    d, slots = Drain(), threading.Semaphore(1)
    slots.acquire()  # the only slot is busy with a running job
    client = ClaimClient(d, [JOB])
    threading.Timer(0.3, d.on_signal, args=(signal.SIGTERM,)).start()
    t = threading.Thread(
        target=main._claim_loop, args=(client, Settings.from_env(), d, slots, lambda j: None)
    )
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert client.claims == 0


# --- reports ----------------------------------------------------------------------------------


class ReportClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict | None]] = []

    def event(self, job_id: str, name: str, phase: str | None = None, result: dict | None = None):  # type: ignore[no-untyped-def]
        self.events.append((name, phase, result))


def _run_stopped_job(monkeypatch: pytest.MonkeyPatch, token: cancel.CancelToken) -> ReportClient:
    def stopped(*_a: object, **_k: object) -> None:
        raise cancel.JobCanceled

    monkeypatch.setattr(main, "pick", lambda *a, **k: None)
    monkeypatch.setattr(main, "handle_plan", stopped)
    monkeypatch.setattr(Settings, "available_tools", lambda self: {})
    client = ReportClient()
    cancel.bind(token)
    try:
        main._run_job(client, JOB, Settings.from_env())  # type: ignore[arg-type]
    finally:
        cancel.bind(None)
    return client


def test_a_job_stopped_by_shutdown_reports_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nobody asked the API to cancel it: `job_canceled` would be refused, so it fails — now,
    rather than at the `worker_lost` reclaim."""
    jobs = cancel.JobRegistry()
    token = jobs.register("run-1")
    assert jobs.shutdown() == 1
    assert token.requested and token.by_shutdown_only
    client = _run_stopped_job(monkeypatch, token)
    assert [e[0] for e in client.events] == ["job_failed"]
    assert client.events[0][2]["error"].startswith("worker_shutdown:")  # type: ignore[index]


def test_a_job_also_canceled_by_the_api_reports_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = cancel.JobRegistry()
    token = jobs.register("run-1")
    jobs.cancel("run-1")
    jobs.shutdown()
    assert not token.by_shutdown_only
    client = _run_stopped_job(monkeypatch, token)
    assert [e[0] for e in client.events] == ["job_canceled"]


def test_a_job_registered_after_the_shutdown_is_stopped_before_it_starts() -> None:
    jobs = cancel.JobRegistry()
    jobs.shutdown()
    token = jobs.register("late")
    assert token.should_stop and token.by_shutdown_only


# --- the whole agent, with real signals --------------------------------------------------------


class FakeApi:
    """The worker API as `run()` uses it: the first claim hands `JOB`, later ones nothing."""

    instance: FakeApi

    def __init__(self, base_url: str) -> None:
        FakeApi.instance = self
        self.lock = threading.Lock()
        self.claims: list[float] = []
        self.heartbeats: list[float] = []
        self.events: list[tuple[str, dict | None, float]] = []

    def register(self, *a: object, **k: object) -> str:
        return "wt"

    def heartbeat(self, in_flight: int = 0, capacity: int = 1) -> list[dict]:
        with self.lock:
            self.heartbeats.append(time.monotonic())
        return []

    def claim(self, wait: int) -> dict | None:
        with self.lock:
            self.claims.append(time.monotonic())
            first = len(self.claims) == 1
        if first:
            return dict(JOB)
        time.sleep(0.05)
        return None

    def event(self, job_id: str, name: str, phase: str | None = None, result: dict | None = None):  # type: ignore[no-untyped-def]
        with self.lock:
            self.events.append((name, result, time.monotonic()))


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """`run()` against `FakeApi`, its plan job blocked until the returned event is set — or until
    its cancel token asks it to stop (as the runners do)."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    release = threading.Event()

    def plan(client: Any, job: dict, settings: Settings, checks: list) -> None:
        token = cancel.current()
        assert token is not None
        while not release.is_set():
            if token.should_stop:
                raise cancel.JobCanceled
            time.sleep(0.02)
        client.event(job["job_id"], "phase_finished", phase="checking", result={})

    monkeypatch.setenv("STACKD_POOL_TOKEN", "pool")
    monkeypatch.setenv("STACKD_HEARTBEAT_INTERVAL", "1")
    monkeypatch.setenv("STACKD_CANCEL_GRACE_SECONDS", "5")
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "ApiClient", FakeApi)
    monkeypatch.setattr(main, "pick", lambda *a, **k: None)
    monkeypatch.setattr(main, "handle_plan", plan)
    monkeypatch.setattr(Settings, "available_tools", lambda self: {})
    try:
        yield release
    finally:
        release.set()
        for s, h in saved.items():
            signal.signal(s, h)


def _kill_later(delay: float) -> float:
    at = time.monotonic() + delay
    threading.Timer(delay, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
    return at


def test_sigterm_drains_then_exits_0(agent_env: threading.Event) -> None:
    signaled = _kill_later(0.5)
    threading.Timer(2.5, agent_env.set).start()  # the job ends 2 s into the drain
    assert main.run() == 0
    api = FakeApi.instance
    assert [e[0] for e in api.events] == ["phase_finished"]  # finished and reported before exit
    assert all(t < signaled + 0.1 for t in api.claims)  # no claim started after the signal
    assert any(t > signaled + 0.5 for t in api.heartbeats)  # still beating while draining


def test_sigterm_past_the_drain_timeout_stops_the_job(
    agent_env: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STACKD_DRAIN_TIMEOUT_SECONDS", "1")
    signaled = _kill_later(0.3)
    assert main.run() == 1
    api = FakeApi.instance
    assert [e[0] for e in api.events] == ["job_failed"]
    assert api.events[0][1]["error"].startswith("worker_shutdown:")  # type: ignore[index]
    assert api.events[0][2] - signaled >= 1  # not before the timeout


def test_a_second_sigterm_stops_at_once(agent_env: threading.Event) -> None:
    _kill_later(0.3)
    second = _kill_later(0.8)
    started = time.monotonic()
    assert main.run() == 1  # the default drain timeout is 20 min
    api = FakeApi.instance
    assert [e[0] for e in api.events] == ["job_failed"]
    assert api.events[0][2] - second < 2
    assert time.monotonic() - started < 10
