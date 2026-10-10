from __future__ import annotations

import threading
import time
from collections import OrderedDict
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


# The webhook limiter is on an unauthenticated route, so distinct source IPs would grow the dict
# without bound. It is an LRU hard-capped at _MAX_BUCKETS (O(1) per request): a new key at the cap
# evicts the least-recently-used bucket. Eviction resets that key — it comes back with a full
# bucket — so a client cycling through more than _MAX_BUCKETS keys can outrun its limit; the cap
# bounds memory and CPU, not the rate of such a client.
_buckets: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()
_MAX_BUCKETS = 10_000
# `_dep` is a plain def, so FastAPI runs it in its threadpool: the whole get/refill/take/evict
# sequence for a key runs under this lock (`now` too, so a bucket's clock never goes backwards).
_lock = threading.Lock()


def _reset() -> None:
    """Clear all buckets — tests only."""
    with _lock:
        _buckets.clear()


def rate_limit(name: str, per_minute: int, burst: int | None = None) -> Callable[[Request], None]:
    """A FastAPI dependency that allows `per_minute` requests per client IP for `name`, tolerating
    a short `burst`. Returns 429 (problem+json) when the bucket is empty."""
    capacity = float(burst if burst is not None else per_minute)
    refill_per_sec = per_minute / 60.0

    def _dep(request: Request) -> None:
        ip = request.client.host if request.client else "?"
        key = (name, ip)
        with _lock:
            now = time.monotonic()
            bucket = _buckets.get(key)
            if bucket is None:
                while len(_buckets) >= _MAX_BUCKETS:
                    _buckets.popitem(last=False)
                bucket = _Bucket(capacity, now)
                _buckets[key] = bucket
            else:
                _buckets.move_to_end(key)
            bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated) * refill_per_sec)
            bucket.updated = now
            allowed = bucket.tokens >= 1.0
            if allowed:
                bucket.tokens -= 1.0
        if not allowed:
            raise ProblemException(
                429, "Too Many Requests", f"Rate limit exceeded for {name}; slow down."
            )

    return _dep
