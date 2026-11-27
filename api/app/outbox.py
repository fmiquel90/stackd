from __future__ import annotations

import email.utils
import random
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_settings

# Retry schedule shared by the notification and VCS outboxes (§7.5).

# A receiver's Retry-After is honoured up to this: a broken or hostile header must not park a row
# for days.
_MAX_RETRY_AFTER = 3600.0
_JITTER = 0.2


def retry_after_seconds(exc: BaseException, now: datetime) -> float | None:
    """The delay a throttling receiver asked for: `Retry-After` (seconds or an HTTP date — Slack's
    429, GitHub's secondary rate limit), else GitHub's primary limit (`x-ratelimit-remaining: 0`
    with the `x-ratelimit-reset` epoch). None when the error carries no such hint."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    headers = exc.response.headers
    value = headers.get("retry-after", "").strip()
    if value.isdigit():
        return float(value)
    if value:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max((when - now).total_seconds(), 0.0)
    reset = headers.get("x-ratelimit-reset", "").strip()
    if headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
        return max(int(reset) - now.timestamp(), 0.0)
    return None


def next_attempt_at(now: datetime, attempt: int, retry_after: float | None = None) -> datetime:
    """When a row that just made its `attempt`-th try is due again: exponential backoff with
    jitter (so a burst of failures doesn't retry in lockstep), or the receiver's Retry-After when
    that is later."""
    settings = get_settings()
    delay = min(
        settings.stackd_outbox_backoff_base_seconds * 2 ** (attempt - 1),
        settings.stackd_outbox_backoff_max_seconds,
    ) * random.uniform(1 - _JITTER, 1 + _JITTER)
    if retry_after is not None:
        delay = max(delay, min(retry_after, _MAX_RETRY_AFTER))
    return now + timedelta(seconds=delay)
