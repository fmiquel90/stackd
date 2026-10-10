from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

from app import ratelimit
from app.errors import ProblemException
from app.ratelimit import _reset, rate_limit
from app.stacks.git import enforce_clone_budget


class _FakeRequest:
    def __init__(self, host: str) -> None:
        self.client = type("C", (), {"host": host})()


def test_token_bucket_allows_burst_then_blocks() -> None:
    _reset()
    dep = rate_limit("t", per_minute=60, burst=3)
    req = _FakeRequest("1.2.3.4")
    dep(req)
    dep(req)
    dep(req)  # burst of 3 in the same instant is allowed
    with pytest.raises(ProblemException) as exc:
        dep(req)
    assert exc.value.status == 429


def test_token_bucket_is_per_ip() -> None:
    _reset()
    dep = rate_limit("t2", per_minute=60, burst=1)
    dep(_FakeRequest("10.0.0.1"))
    dep(_FakeRequest("10.0.0.2"))  # a different IP has its own bucket → not blocked


def test_bucket_dict_never_exceeds_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset()
    monkeypatch.setattr(ratelimit, "_MAX_BUCKETS", 5)
    dep = rate_limit("cap", per_minute=60)
    for i in range(50):  # none idle: every bucket is fresh, the cap must still hold
        dep(_FakeRequest(f"10.1.0.{i}"))
        assert len(ratelimit._buckets) <= 5
    assert len(ratelimit._buckets) == 5


def test_eviction_drops_least_recently_used(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset()
    monkeypatch.setattr(ratelimit, "_MAX_BUCKETS", 3)
    dep = rate_limit("lru", per_minute=1, burst=2)  # ~no refill during the test
    for ip in ("a", "b", "c"):
        dep(_FakeRequest(ip))
    dep(_FakeRequest("a"))  # touching "a" makes "b" the oldest
    dep(_FakeRequest("d"))  # at the cap → evicts "b", not "a"
    assert list(ratelimit._buckets) == [("lru", "c"), ("lru", "a"), ("lru", "d")]
    with pytest.raises(ProblemException):  # "a" kept its (now empty) bucket
        dep(_FakeRequest("a"))


def test_concurrent_new_keys_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_dep` runs in FastAPI's threadpool: concurrent inserts/evictions at the cap must neither
    # crash (dict changed size during iteration / double delete) nor grow the dict past the cap.
    _reset()
    monkeypatch.setattr(ratelimit, "_MAX_BUCKETS", 50)
    dep = rate_limit("stress", per_minute=60, burst=1)
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def hammer(t: int) -> None:
        start.wait()
        for i in range(1000):
            try:
                dep(_FakeRequest(f"{t}.{i % 200}"))
            except ProblemException:
                pass  # a 429 is a legitimate outcome
            except BaseException as e:
                errors.append(e)
                return

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    assert len(ratelimit._buckets) <= 50


async def test_metrics_endpoint_exposes_prometheus(client: httpx.AsyncClient) -> None:
    # Unauthenticated scrape endpoint; bounded gauges only, never secrets/tfvars.
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "stackd_runs_total" in body
    assert "stackd_queue_depth" in body
    assert "stackd_workers_online" in body


async def test_health_requires_auth_metrics_does_not(client: httpx.AsyncClient) -> None:
    assert (await client.get("/metrics")).status_code == 200
    assert (await client.get("/api/v1/health")).status_code == 401


def test_clone_budget_rejects_oversized(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"0" * 4096)
    enforce_clone_budget(tmp_path, max_mb=1)  # under cap → ok
    with pytest.raises(ProblemException) as exc:
        enforce_clone_budget(tmp_path, max_mb=0)  # 0 MB cap → over budget
    assert exc.value.status == 413
