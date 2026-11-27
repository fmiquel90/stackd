"""Retry pacing (SPECS §7.1): a failed claim or registration is retried after a capped exponential
backoff with jitter, reset on success; a runner image pull is bounded in time."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

import agent.main as main
from agent.backoff import Backoff
from agent.config import Settings
from agent.docker_engine import DockerEngine, DockerError
from agent.drain import Drain


def test_backoff_grows_is_capped_jittered_and_resets() -> None:
    b = Backoff(base=1.0, cap=8.0)
    delays = [b.next() for _ in range(6)]
    for delay, step in zip(delays, [1, 2, 4, 8, 8, 8]):
        assert step / 2 <= delay <= step
    b.reset()
    assert b.next() <= 1.0


def test_backoff_spreads_a_fleet() -> None:
    delays = {round(Backoff(base=10.0, cap=10.0).next(), 3) for _ in range(20)}
    assert len(delays) > 1


class _FailingClaims:
    def __init__(self, drain: Drain, outcomes: list[Any]) -> None:
        self.drain, self.outcomes = drain, outcomes

    def claim(self, wait: int) -> dict | None:
        outcome = self.outcomes.pop(0)
        if not self.outcomes:
            self.drain.on_signal(15)  # last one: stop the loop
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_claim_errors_back_off_and_a_success_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(Drain, "sleep", lambda self, seconds, step=0.2: sleeps.append(seconds))
    monkeypatch.setenv("STACKD_HEARTBEAT_INTERVAL", "20")
    d = Drain()
    down = httpx.ConnectError("api down")
    client = _FailingClaims(d, [down, down, down, down, None, down, None])
    main._claim_loop(client, Settings.from_env(), d, threading.Semaphore(1), lambda j: None)  # type: ignore[arg-type]
    assert len(sleeps) == 5
    first, second, third, fourth, after_reset = sleeps
    assert first <= 1.0 and 1.0 <= second <= 2.0 and 2.0 <= third <= 4.0 and 4.0 <= fourth <= 8.0
    assert after_reset <= 1.0  # the claim that got through reset the backoff


def test_registration_retries_back_off(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    tries = {"n": 0}

    class Api:
        def __init__(self, url: str) -> None:
            pass

        def register(self, *a: Any, **kw: Any) -> str:
            tries["n"] += 1
            raise httpx.ConnectError("api not up yet")

    def sleep(self: Drain, seconds: float, step: float = 0.2) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 4:
            self.on_signal(15)

    monkeypatch.setenv("STACKD_POOL_TOKEN", "pool")
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "_reap_orphans", lambda settings: None)
    monkeypatch.setattr(main, "ApiClient", Api)
    monkeypatch.setattr(main.signal, "signal", lambda *a: None)
    monkeypatch.setattr(Drain, "sleep", sleep)
    monkeypatch.setattr(Settings, "available_tools", lambda self: {})
    assert main.run() == 0
    assert tries["n"] == 4
    assert sleeps[0] <= 1.0 and sleeps[3] >= 4.0  # not a fixed 20 s


# --- the runner image pull ----------------------------------------------------------------------


def test_a_slow_pull_fails_past_its_timeout() -> None:
    def slow() -> Iterator[bytes]:
        for _ in range(50):
            time.sleep(0.1)
            yield b'{"status":"Downloading"}\n'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/images/create":
            return httpx.Response(200, content=slow())
        return httpx.Response(404)

    engine = DockerEngine()
    engine._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")
    started = time.monotonic()
    with pytest.raises(DockerError, match="timed out after 1s"):
        engine.pull("registry.example.com/x@sha256:" + "a" * 64, timeout=1)
    assert time.monotonic() - started < 3


def test_the_pull_timeout_is_a_setting_cut_to_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from agent.deadline import Deadline

    timeouts: list[float] = []

    class Engine:
        def __init__(self, docker_host: str | None = None) -> None:
            pass

        def image_exists(self, ref: str) -> bool:
            return False

        def pull(self, ref: str, credential: Any = None, *, timeout: float) -> None:
            timeouts.append(timeout)

    image = "registry.example.com/stackd/opentofu@sha256:" + "a" * 64
    monkeypatch.setenv("STACKD_RUNNER", "docker")
    monkeypatch.setenv("STACKD_RUNNER_IMAGES", json.dumps({"opentofu:1.12.2": image}))
    monkeypatch.setattr(main, "DockerEngine", Engine)
    monkeypatch.setattr(main, "DockerRunner", lambda *a, **k: object())
    job = {"job_id": "run-1", "environment": {"tool": "opentofu", "tool_version": "1.12.2"}}

    class Ws:
        path = tmp_path

    assert Settings.from_env().docker_pull_timeout_seconds == 600
    monkeypatch.setenv("STACKD_DOCKER_PULL_TIMEOUT_SECONDS", "120")
    settings = Settings.from_env()
    main._make_runner(settings, job, Ws())  # type: ignore[arg-type]
    main._make_runner(settings, job, Ws(), Deadline(time.monotonic() + 100))  # type: ignore[arg-type]
    assert timeouts[0] == 120 and timeouts[1] <= 40
