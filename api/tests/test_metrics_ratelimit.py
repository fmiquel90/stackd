from __future__ import annotations

from pathlib import Path

import httpx
import pytest

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
