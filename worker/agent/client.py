from __future__ import annotations

import httpx


class ApiClient:
    """Thin client for the worker protocol (SPECS §7). All calls are pull/outbound."""

    def __init__(self, base_url: str) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=120)
        self._worker_token: str | None = None
        # Kept so an expired/rotated worker token (HTTP 401) can be refreshed transparently by
        # re-registering, instead of spinning on 401 until the process is restarted.
        self._pool_token: str | None = None
        self._name: str | None = None
        self._labels: dict | None = None

    def register(self, pool_token: str, name: str, labels: dict | None = None) -> str:
        self._pool_token, self._name, self._labels = pool_token, name, labels
        resp = self._http.post(
            "/worker/v1/register",
            headers={"Authorization": f"Bearer {pool_token}"},
            json={"name": name, "labels": labels},
        )
        resp.raise_for_status()
        self._worker_token = resp.json()["worker_token"]
        return self._worker_token

    def _auth(self) -> dict[str, str]:
        assert self._worker_token is not None
        return {"Authorization": f"Bearer {self._worker_token}"}

    def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Authed request that refreshes the worker token once on 401 (expiry/rotation) and retries.
        Re-registration reuses the (pool, name) row, so worker identity / run ownership is stable."""
        resp = self._http.request(method, url, headers=self._auth(), **kwargs)  # type: ignore[arg-type]
        if resp.status_code == 401 and self._pool_token is not None and self._name is not None:
            self.register(self._pool_token, self._name, self._labels)
            resp = self._http.request(method, url, headers=self._auth(), **kwargs)  # type: ignore[arg-type]
        return resp

    def heartbeat(self, in_flight: int = 0, capacity: int = 1) -> list[dict]:
        resp = self._send(
            "POST", "/worker/v1/heartbeat", json={"in_flight": in_flight, "capacity": capacity}
        )
        resp.raise_for_status()
        return resp.json().get("commands", [])

    def command_result(self, command_id: str, result: dict, status: str = "done") -> None:
        self._send(
            "POST",
            f"/worker/v1/commands/{command_id}/result",
            json={"status": status, "result": result},
        ).raise_for_status()

    def claim(self, wait: int) -> dict | None:
        resp = self._send("POST", f"/worker/v1/jobs/claim?wait={wait}")
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()

    def event(
        self, job_id: str, event: str, *, phase: str | None = None, result: dict | None = None
    ) -> None:
        self._send(
            "POST",
            f"/worker/v1/jobs/{job_id}/events",
            json={"event": event, "phase": phase, "result": result},
        ).raise_for_status()

    def logs(
        self, job_id: str, phase: str, seq: int, lines: list[dict], section: str | None = None
    ) -> None:
        self._send(
            "POST",
            f"/worker/v1/jobs/{job_id}/logs",
            json={"phase": phase, "section": section, "seq": seq, "lines": lines},
        ).raise_for_status()

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self._send(
            "PUT", f"/worker/v1/jobs/{job_id}/artifacts/{name}", content=data
        ).raise_for_status()
