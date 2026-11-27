"""The worker→API client's retry policy (SPECS §7): transient failures (transport errors, 5xx, 429)
are retried with backoff for a bounded time; a call that is not safe to repeat (the claim) only
retries what the API provably never processed; a terminal event repeated after an ambiguous attempt
takes the API's 409 as its acknowledgement; a 401 re-reads a rotated pool token."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from agent.client import ApiClient

Handler = Callable[[httpx.Request], httpx.Response]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(handler: Handler, *, retry_for: float = 60.0) -> tuple[ApiClient, _Clock]:
    clock = _Clock()
    client = ApiClient("http://api", retry_for=retry_for, sleep=clock.sleep, clock=clock)
    client._http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    client._worker_token = "w"
    return client, clock


def _script(*answers: int | type[Exception]) -> tuple[Handler, list[httpx.Request]]:
    """A handler answering each request with the next status (or raising the next error); the
    last answer repeats."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers[min(len(seen), len(answers)) - 1]
        if isinstance(answer, int):
            return httpx.Response(answer, json={"title": "x"})
        raise answer("boom", request=request)

    return handler, seen


def test_logs_retry_5xx_and_transport_errors() -> None:
    handler, seen = _script(503, httpx.ConnectError, httpx.ReadTimeout, 502, 202)
    client, clock = _client(handler)
    client.logs("run-1", "planning", 0, [{"t": "", "msg": "x"}])
    assert len(seen) == 5
    assert len(clock.sleeps) == 4
    assert all(0 < s <= 15 for s in clock.sleeps)


def test_retry_after_is_honoured() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(202, json={})

    client, clock = _client(handler)
    client.logs("run-1", "planning", 0, [])
    assert clock.sleeps == [7.0]


def test_gives_up_once_the_retry_budget_is_spent() -> None:
    handler, seen = _script(httpx.ConnectError)
    client, clock = _client(handler, retry_for=20)
    with pytest.raises(httpx.ConnectError):
        client.event("run-1", "phase_started", phase="planning")
    assert clock.now <= 20
    assert len(seen) > 2

    handler, _ = _script(500)
    client, clock = _client(handler, retry_for=20)
    with pytest.raises(httpx.HTTPStatusError):
        client.command_result("cmd-1", {})
    assert clock.now <= 20


def test_the_saved_plan_download_retries_a_503_then_fails_closed() -> None:
    # The API answers 503 (retryable) while its object store is unavailable.
    handler, seen = _script(503, 200)
    client, _ = _client(handler)
    assert client.download_artifact("run-1", "plan.tfplan") == b'{"title":"x"}'
    assert len(seen) == 2

    handler, seen = _script(503)
    client, clock = _client(handler, retry_for=20)
    with pytest.raises(RuntimeError, match=r"^HTTP 503"):
        client.download_artifact("run-1", "plan.tfplan")
    assert len(seen) > 2 and clock.now <= 20


def test_a_client_error_is_not_retried() -> None:
    handler, seen = _script(422)
    client, clock = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.upload_artifact("run-1", "plan.json", b"{}")
    assert len(seen) == 1 and clock.sleeps == []


def test_the_claim_is_not_retried_once_it_may_have_been_processed() -> None:
    # Sent, answer lost: the API may have handed this worker a run — another claim would take a
    # second one. The claim loop decides what to do next.
    handler, seen = _script(httpx.ReadTimeout)
    client, _ = _client(handler)
    with pytest.raises(httpx.ReadTimeout):
        client.claim(wait=25)
    assert len(seen) == 1

    handler, seen = _script(502)
    client, _ = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.claim(wait=25)
    assert len(seen) == 1


def test_the_claim_retries_what_the_api_never_processed() -> None:
    # Never connected, or the API's "signing unavailable, retry" 503 (§7.2: the claim was undone).
    handler, seen = _script(httpx.ConnectError, 503, 204)
    client, _ = _client(handler)
    assert client.claim(wait=25) is None
    assert len(seen) == 3


def test_the_claim_long_poll_gets_its_wait_on_top_of_the_timeout() -> None:
    handler, seen = _script(204)
    client, _ = _client(handler)
    client.claim(wait=25)
    assert seen[0].extensions["timeout"]["read"] == 55.0
    handler, seen = _script(202)
    client, _ = _client(handler)
    client.logs("run-1", "planning", 0, [])
    assert seen[0].extensions["timeout"]["read"] == 30.0


@pytest.mark.parametrize("first", [httpx.ReadTimeout, 504])
def test_a_repeated_terminal_event_takes_the_409_as_its_acknowledgement(
    first: int | type[Exception],
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            if isinstance(first, int):
                return httpx.Response(first)
            raise first("lost", request=request)  # processed, the answer lost
        return httpx.Response(409, json={"title": "Illegal transition"})

    client, _ = _client(handler)
    client.event("run-1", "phase_finished", result={"outputs": {}})  # no raise
    assert len(calls) == 2


def test_a_409_is_an_error_when_no_earlier_attempt_may_have_been_processed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"title": "Job not executing"})

    client, _ = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.event("run-1", "job_failed", result={})


def test_a_409_for_a_run_reclaimed_by_another_worker_is_an_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(502)
        return httpx.Response(409, json={"title": "Not your job"})

    client, _ = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.event("run-1", "phase_finished", result={})


def test_a_401_re_reads_a_rotated_pool_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "pool.token"
    token_file.write_text("pool-old\n")
    monkeypatch.delenv("STACKD_POOL_TOKEN", raising=False)
    monkeypatch.setenv("STACKD_POOL_TOKEN_FILE", str(token_file))
    registered_with: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["Authorization"]
        if request.url.path == "/worker/v1/register":
            registered_with.append(auth)
            if auth != "Bearer pool-new":
                return httpx.Response(401)
            return httpx.Response(200, json={"worker_id": "w", "worker_token": "w2"})
        if auth != "Bearer w2":
            return httpx.Response(401)
        return httpx.Response(200, json={"commands": []})

    client, _ = _client(handler)
    client._pool_token, client._name = "pool-old", "agent-1"
    token_file.write_text("pool-new\n")  # rotated while the agent runs
    assert client.heartbeat() == []
    assert registered_with == ["Bearer pool-new"]
    assert client._pool_token == "pool-new"


def test_a_transient_failure_of_the_401_re_registration_is_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/worker/v1/register":
            if calls.count("/worker/v1/register") == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"worker_id": "w", "worker_token": "w2"})
        if request.headers["Authorization"] != "Bearer w2":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    client, _ = _client(handler)
    client._pool_token, client._name = "pool", "agent-1"
    client.command_result("cmd-1", {"x": 1})
    assert calls.count("/worker/v1/register") == 2
