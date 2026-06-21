from __future__ import annotations

import threading

from agent.config import Settings
from agent.main import _InFlight


def test_inflight_counts_concurrent_threads() -> None:
    inflight = _InFlight()
    started = threading.Barrier(5)
    release = threading.Event()

    def worker() -> None:
        inflight.inc()
        started.wait()
        release.wait()
        inflight.dec()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    inflight.inc()
    started.wait()  # all 5 incremented
    assert inflight.count() == 5
    release.set()
    for t in threads:
        t.join()
    inflight.dec()
    assert inflight.count() == 0


def test_max_concurrent_jobs_defaults_to_one(monkeypatch) -> None:
    monkeypatch.delenv("STACKD_MAX_CONCURRENT_JOBS", raising=False)
    assert Settings.from_env().max_concurrent_jobs == 1


def test_max_concurrent_jobs_floor_of_one(monkeypatch) -> None:
    monkeypatch.setenv("STACKD_MAX_CONCURRENT_JOBS", "0")
    assert Settings.from_env().max_concurrent_jobs == 1
    monkeypatch.setenv("STACKD_MAX_CONCURRENT_JOBS", "4")
    assert Settings.from_env().max_concurrent_jobs == 4
