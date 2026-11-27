"""The heartbeat (SPECS §7.1): it lists the jobs running here, never waits long on the API, and
handles each command it brings back on its own — diagnostics off the heartbeat thread."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from agent import cancel
from agent.client import HEARTBEAT_TIMEOUT_SECONDS, ApiClient
from agent.config import Settings
from agent.main import _beat, _InFlight


class FakeClient:
    def __init__(self, commands: list[dict], failing: set[str] | None = None) -> None:
        self.commands = commands
        self.failing = failing or set()
        self.beats: list[dict] = []
        self.results: list[tuple[str, dict, str]] = []
        self.lock = threading.Lock()

    def heartbeat(
        self, in_flight: int = 0, capacity: int = 1, job_ids: list[str] | None = None
    ) -> list[dict]:
        self.beats.append({"in_flight": in_flight, "job_ids": job_ids})
        return self.commands

    def command_result(self, command_id: str, result: dict, status: str = "done") -> None:
        if command_id in self.failing:
            raise httpx.ConnectError("API unreachable")
        with self.lock:
            self.results.append((command_id, result, status))


def test_heartbeat_lists_running_jobs_with_a_short_timeout() -> None:
    seen: list[tuple[dict, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/worker/v1/register":
            return httpx.Response(200, json={"worker_id": "w", "worker_token": "t"})
        seen.append((json.loads(request.content), request.extensions["timeout"]))
        return httpx.Response(200, json={"commands": []})

    client = ApiClient("http://api")
    client._http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    client.register("pool", "agent-1")

    jobs = cancel.JobRegistry()
    jobs.register("run-1")
    jobs.register("run-2")
    jobs.unregister("run-1")
    inflight = _InFlight()
    inflight.inc()
    _beat(client, Settings.from_env(), inflight, jobs)

    body, timeout = seen[0]
    assert body["job_ids"] == ["run-2"] and body["in_flight"] == 1
    # Before, the beat used the client's 120 s timeout: a stuck API kept the heartbeat thread
    # waiting past the API's offline threshold.
    assert timeout["read"] == HEARTBEAT_TIMEOUT_SECONDS
    assert HEARTBEAT_TIMEOUT_SECONDS <= 10

    client.heartbeat()  # no registry: no listing, as before
    assert "job_ids" not in seen[1][0]


def test_one_failing_command_does_not_drop_the_others() -> None:
    """Before, the whole batch was one try: a result post failing dropped the commands after it —
    a stop request among them."""
    jobs = cancel.JobRegistry()
    first, second = jobs.register("run-1"), jobs.register("run-2")
    client = FakeClient(
        [
            {"id": "c1", "type": "cancel_job", "payload": {"job_id": "run-1"}},
            {"id": "c2", "type": "cancel_job", "payload": {"job_id": "run-2"}},
        ],
        failing={"c1"},
    )
    _beat(client, Settings.from_env(), _InFlight(), jobs)  # type: ignore[arg-type]
    assert first.requested and second.requested
    assert client.results == [("c2", {"job_id": "run-2", "running_here": True}, "done")]


def test_diagnostics_run_off_the_heartbeat_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before, the bundle's subprocesses (up to 4 × 10 s) ran on the heartbeat thread, long enough
    for the API to see the worker offline."""
    import agent.diagnostics

    release = threading.Event()
    calls: list[str] = []

    def slow_collect(api_url: str, runner: str) -> dict:
        calls.append(api_url)
        release.wait(5)
        return {"platform": "test"}

    monkeypatch.setattr(agent.diagnostics, "collect", slow_collect)
    client = FakeClient([{"id": "d1", "type": "diagnostics", "payload": None}])
    settings = Settings.from_env()

    started = time.monotonic()
    _beat(client, settings, _InFlight())  # type: ignore[arg-type]
    # Delivered again while still collecting (its result is late): not run twice.
    _beat(client, settings, _InFlight())  # type: ignore[arg-type]
    assert time.monotonic() - started < 1
    assert client.results == []

    release.set()
    deadline = time.monotonic() + 5
    while not client.results and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.results == [("d1", {"platform": "test"}, "done")]
    assert len(calls) == 1
