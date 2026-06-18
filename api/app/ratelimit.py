from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import Request

from app.errors import ProblemException

# In-process token-bucket rate limiting (SPECS §H). Per-replica (no Redis in the MVP); buckets are
# keyed by (limiter name, client IP). Good enough to blunt brute-force / abuse on the few sensitive
# routes — auth, the public webhook, and the repo-cloning discovery endpoint.


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: float, now: float) -> None:
        self.tokens = capacity
        self.updated = now


_buckets: dict[tuple[str, str], _Bucket] = {}


def _reset() -> None:
    """Clear all buckets — tests only."""
    _buckets.clear()


def rate_limit(name: str, per_minute: int, burst: int | None = None) -> Callable[[Request], None]:
    """A FastAPI dependency that allows `per_minute` requests per client IP for `name`, tolerating
    a short `burst`. Returns 429 (problem+json) when the bucket is empty."""
    capacity = float(burst if burst is not None else per_minute)
    refill_per_sec = per_minute / 60.0

    def _dep(request: Request) -> None:
        ip = request.client.host if request.client else "?"
        key = (name, ip)
        now = time.monotonic()
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _Bucket(capacity, now)
            _buckets[key] = bucket
        bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated) * refill_per_sec)
        bucket.updated = now
        if bucket.tokens < 1.0:
            raise ProblemException(
                429, "Too Many Requests", f"Rate limit exceeded for {name}; slow down."
            )
        bucket.tokens -= 1.0

    return _dep
