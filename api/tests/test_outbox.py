from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.outbox import next_attempt_at, retry_after_seconds

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _error(status: int, headers: dict[str, str]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/hook")
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def test_retry_after_forms() -> None:
    assert retry_after_seconds(_error(429, {"Retry-After": "30"}), NOW) == 30
    date = format_datetime(NOW + timedelta(seconds=90), usegmt=True)
    assert retry_after_seconds(_error(503, {"Retry-After": date}), NOW) == 90
    reset = str(int(NOW.timestamp()) + 600)  # GitHub primary rate limit
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}
    assert retry_after_seconds(_error(403, headers), NOW) == 600
    assert retry_after_seconds(_error(403, {"x-ratelimit-remaining": "12"}), NOW) is None
    assert retry_after_seconds(_error(500, {}), NOW) is None
    assert retry_after_seconds(_error(429, {"Retry-After": "soon"}), NOW) is None
    assert retry_after_seconds(httpx.ConnectError("refused"), NOW) is None


@pytest.mark.parametrize(
    ("attempt", "base"),
    [(1, 10), (2, 20), (4, 80), (7, 640), (8, 900), (11, 900)],  # doubling, capped at 900 s
)
def test_backoff_doubles_up_to_the_cap_with_jitter(attempt: int, base: float) -> None:
    for _ in range(50):
        delay = (next_attempt_at(NOW, attempt) - NOW).total_seconds()
        assert base * 0.8 <= delay <= base * 1.2


def test_default_budget_spans_over_an_hour() -> None:
    from app.config import get_settings

    retries = get_settings().stackd_outbox_max_attempts - 1
    shortest = sum(
        min(10 * 2 ** (n - 1), 900) * 0.8 for n in range(1, retries + 1)
    )  # every jitter at its low end
    assert shortest >= 3600


def test_retry_after_wins_when_later_and_is_capped() -> None:
    assert next_attempt_at(NOW, 1, retry_after=300) == NOW + timedelta(seconds=300)
    assert next_attempt_at(NOW, 1, retry_after=86400) == NOW + timedelta(hours=1)
    assert (next_attempt_at(NOW, 8, retry_after=5) - NOW).total_seconds() >= 720
