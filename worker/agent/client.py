from __future__ import annotations

import httpx


class ApiClient:
    """Thin client for the worker protocol (SPECS §7). All calls are pull/outbound."""

    def __init__(self, base_url: str) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=40)
        self._worker_token: str | None = None

    def register(self, pool_token: str, name: str, labels: dict | None = None) -> str:
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

    def heartbeat(self) -> list[dict]:
        resp = self._http.post("/worker/v1/heartbeat", headers=self._auth())
        resp.raise_for_status()
        return resp.json().get("commands", [])

    def command_result(self, command_id: str, result: dict, status: str = "done") -> None:
        self._http.post(
            f"/worker/v1/commands/{command_id}/result",
            headers=self._auth(),
            json={"status": status, "result": result},
        ).raise_for_status()

    def claim(self, wait: int) -> dict | None:
        resp = self._http.post(f"/worker/v1/jobs/claim?wait={wait}", headers=self._auth())
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()

    def event(
        self, job_id: str, event: str, *, phase: str | None = None, result: dict | None = None
    ) -> None:
        self._http.post(
            f"/worker/v1/jobs/{job_id}/events",
            headers=self._auth(),
            json={"event": event, "phase": phase, "result": result},
        ).raise_for_status()

    def logs(
        self, job_id: str, phase: str, seq: int, lines: list[dict], section: str | None = None
    ) -> None:
        self._http.post(
            f"/worker/v1/jobs/{job_id}/logs",
            headers=self._auth(),
            json={"phase": phase, "section": section, "seq": seq, "lines": lines},
        ).raise_for_status()

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self._http.put(
            f"/worker/v1/jobs/{job_id}/artifacts/{name}", headers=self._auth(), content=data
        ).raise_for_status()
