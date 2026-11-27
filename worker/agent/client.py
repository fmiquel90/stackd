from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from agent.config import pool_token_from_env
from agent.logging import get_logger

log = get_logger()

# What a request gets to answer. The claim long-poll gets its `wait` on top (claim()).
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Artifacts: the API stores the saved plan (up to its size cap) before it answers.
ARTIFACT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
CLAIM_MARGIN_SECONDS = 30.0
# How long a call keeps retrying a transient failure before it gives up (raises).
RETRY_SECONDS = 60.0
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 15.0

# Statuses worth another try: the API (or a proxy in front of it) is overloaded or restarting.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Statuses that say the request was not processed: the only ones a non-idempotent call retries.
_NOT_PROCESSED_STATUSES = frozenset({429, 503})
# The request never reached the API: safe to resend whatever it is.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Terminal job events (§7.3): the API answers a repeat of one it already recorded with 409 (the
# run has left the executing state). After an attempt that may have been processed, that 409 is
# the acknowledgement. "Not your job" is not: the run was reclaimed by another worker.
_TERMINAL_EVENTS = frozenset({"phase_finished", "job_failed", "job_canceled"})
_REPEAT_TITLES = frozenset({"Job not executing", "Illegal transition"})


def transient(exc: BaseException) -> bool:
    """A failure another attempt may not hit: transport errors, 5xx, 429."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUSES
    return isinstance(exc, httpx.TransportError)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _problem_title(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("title") if isinstance(body, dict) else None


# The heartbeat thread's calls (the beat, a command's result): far below the API's offline
# threshold (60 s), so a stuck API costs a beat or two, never the worker's liveness — both the
# request and its retries.
HEARTBEAT_TIMEOUT_SECONDS = 10.0


class ApiClient:
    """Thin client for the worker protocol (SPECS §7). All calls are pull/outbound. Each call
    retries a transient failure (transport error, 5xx, 429 — honouring Retry-After) with
    exponential backoff and jitter, for up to `retry_for` seconds; a call that is not safe to repeat
    (the claim) only retries what provably was not processed. Thread-safe: the heartbeat, the jobs
    and their log senders share one client."""

    def __init__(
        self,
        base_url: str,
        *,
        retry_for: float = RETRY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT)
        self._retry_for = retry_for
        self._sleep = sleep
        self._clock = clock
        self._worker_token: str | None = None
        # Kept so an expired/rotated worker token (HTTP 401) can be refreshed transparently by
        # re-registering, instead of spinning on 401 until the process is restarted.
        self._pool_token: str | None = None
        self._name: str | None = None
        self._labels: dict | None = None
        self._runner: str | None = None
        self._tools: dict[str, list[str]] | None = None

    def register(
        self,
        pool_token: str,
        name: str,
        labels: dict | None = None,
        runner: str | None = None,
        tools: dict[str, list[str]] | None = None,
    ) -> str:
        self._pool_token, self._name, self._labels = pool_token, name, labels
        if runner is not None:
            self._runner = runner
        if tools is not None:
            self._tools = tools
        resp = self._http.post(
            "/worker/v1/register",
            headers={"Authorization": f"Bearer {pool_token}"},
            # The runner is declared on every (re-)registration: a production API refuses any
            # worker that is not on the `docker` runner (SPECS §7.4).
            # …and the tool versions it can run: the API hands it only runs it can execute.
            json={"name": name, "labels": labels, "runner": self._runner, "tools": self._tools},
        )
        resp.raise_for_status()
        self._worker_token = resp.json()["worker_token"]
        return self._worker_token

    def _auth(self) -> dict[str, str]:
        assert self._worker_token is not None
        return {"Authorization": f"Bearer {self._worker_token}"}

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """One authed attempt, refreshing the worker token once on 401 (expiry/rotation).
        Re-registration reuses the (pool, name) row, so worker identity / run ownership is stable.
        The pool token is re-read first: once rotated, the one cached at boot is refused too."""
        resp = self._http.request(method, url, headers=self._auth(), **kwargs)  # type: ignore[arg-type]
        if resp.status_code == 401 and self._pool_token is not None and self._name is not None:
            self.register(pool_token_from_env() or self._pool_token, self._name, self._labels)
            resp = self._http.request(method, url, headers=self._auth(), **kwargs)  # type: ignore[arg-type]
        return resp

    def _backoff(self, attempt: int) -> float:
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)
        return ceiling / 2 + random.uniform(0, ceiling / 2)

    def _send_tracked(
        self,
        method: str,
        url: str,
        *,
        idempotent: bool = True,
        retry_for: float | None = None,
        **kwargs: object,
    ) -> tuple[httpx.Response, bool]:
        """The response, and whether an earlier attempt may have been processed by the API (a
        request sent whose answer was lost, or a 5xx). Raises the last transport error once the
        retry budget is spent; a retryable status is returned as is."""
        deadline = self._clock() + (self._retry_for if retry_for is None else retry_for)
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        maybe_processed = False
        attempt = 0
        while True:
            error: Exception | None = None
            resp: httpx.Response | None = None
            delay: float | None = None
            try:
                resp = self._request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if not (idempotent or isinstance(exc, _NOT_SENT)):
                    raise
                error = exc
            except httpx.HTTPStatusError as exc:  # the re-registration a 401 triggered
                if not transient(exc):
                    raise
                error = exc
            if resp is not None:
                retryable = _RETRY_STATUSES if idempotent else _NOT_PROCESSED_STATUSES
                if resp.status_code not in retryable:
                    return resp, maybe_processed
                delay = _retry_after(resp)
            if delay is None:
                delay = self._backoff(attempt)
            if self._clock() + delay > deadline:
                if error is not None:
                    raise error
                assert resp is not None
                return resp, maybe_processed
            if (resp is not None and resp.status_code not in _NOT_PROCESSED_STATUSES) or (
                error is not None and not isinstance(error, (*_NOT_SENT, httpx.HTTPStatusError))
            ):
                maybe_processed = True
            log.warning(
                "API call failed, retrying",
                extra={
                    "event": "agent.api_retry",
                    "method": method,
                    "path": url.split("?")[0],
                    "status": resp.status_code if resp is not None else None,
                    "error": type(error).__name__ if error is not None else None,
                    "retry_in": round(delay, 2),
                },
            )
            self._sleep(delay)
            attempt += 1

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._send_tracked(method, url, **kwargs)[0]

    def heartbeat(
        self, in_flight: int = 0, capacity: int = 1, job_ids: list[str] | None = None
    ) -> list[dict]:
        body: dict[str, object] = {"in_flight": in_flight, "capacity": capacity}
        if job_ids is not None:
            body["job_ids"] = job_ids
        resp = self._send(
            "POST",
            "/worker/v1/heartbeat",
            json=body,
            timeout=HEARTBEAT_TIMEOUT_SECONDS,
            retry_for=HEARTBEAT_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json().get("commands", [])

    def command_result(self, command_id: str, result: dict, status: str = "done") -> None:
        self._send(
            "POST",
            f"/worker/v1/commands/{command_id}/result",
            json={"status": status, "result": result},
            timeout=HEARTBEAT_TIMEOUT_SECONDS,
            retry_for=HEARTBEAT_TIMEOUT_SECONDS,
        ).raise_for_status()

    def claim(self, wait: int) -> dict | None:
        # Not idempotent: a claim whose answer is lost has still handed this worker a run, and a
        # second claim would take another. Only a refusal the API never acted on is retried.
        resp = self._send(
            "POST",
            f"/worker/v1/jobs/claim?wait={wait}",
            idempotent=False,
            timeout=httpx.Timeout(wait + CLAIM_MARGIN_SECONDS, connect=10.0),
        )
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()

    def event(
        self, job_id: str, event: str, *, phase: str | None = None, result: dict | None = None
    ) -> None:
        resp, maybe_processed = self._send_tracked(
            "POST",
            f"/worker/v1/jobs/{job_id}/events",
            json={"event": event, "phase": phase, "result": result},
        )
        if (
            resp.status_code == 409
            and maybe_processed
            and event in _TERMINAL_EVENTS
            and _problem_title(resp) in _REPEAT_TITLES
        ):
            log.info(
                "job event already recorded",
                extra={"event": "agent.event_acknowledged", "run_id": job_id, "job_event": event},
            )
            return
        resp.raise_for_status()

    def logs(
        self,
        job_id: str,
        phase: str,
        seq: int,
        lines: list[dict],
        section: str | None = None,
        *,
        retry_for: float | None = None,
    ) -> None:
        # Idempotent on (run_id, phase, seq) (§5.1): a resent chunk is a no-op.
        self._send(
            "POST",
            f"/worker/v1/jobs/{job_id}/logs",
            json={"phase": phase, "section": section, "seq": seq, "lines": lines},
            retry_for=retry_for,
        ).raise_for_status()

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        # A PUT: a repeat replaces the artifact with the same bytes.
        self._send(
            "PUT",
            f"/worker/v1/jobs/{job_id}/artifacts/{name}",
            content=data,
            timeout=ARTIFACT_TIMEOUT,
        ).raise_for_status()

    def download_artifact(self, job_id: str, name: str) -> bytes:
        """A job's artifact (the apply job's saved plan, §7.4). Any refusal raises, carrying the
        API's problem detail — the caller fails closed on it."""
        resp = self._send(
            "GET", f"/worker/v1/jobs/{job_id}/artifacts/{name}", timeout=ARTIFACT_TIMEOUT
        )
        if resp.status_code != 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("title")
            else:
                detail = resp.text[:200]
            raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
        return resp.content
