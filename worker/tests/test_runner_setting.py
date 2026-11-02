"""The agent's runner setting (SPECS §7.4): only `local` or `docker`, never a silent fallback, and
declared to the API on every registration (a production API refuses anything but `docker`)."""

from __future__ import annotations

import json

import httpx
import pytest

from agent.client import ApiClient
from agent.config import Settings


def test_unknown_runner_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKD_RUNNER", "dockr")
    with pytest.raises(ValueError, match="STACKD_RUNNER"):
        Settings.from_env()


@pytest.mark.parametrize("runner", ["local", "docker"])
def test_known_runners_load(monkeypatch: pytest.MonkeyPatch, runner: str) -> None:
    monkeypatch.setenv("STACKD_RUNNER", runner)
    assert Settings.from_env().runner == runner


def test_register_declares_the_runner_on_every_registration() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"worker_id": "w", "worker_token": "t"})

    client = ApiClient("http://api")
    client._http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    client.register("pool", "agent-1", runner="docker")
    client.register("pool", "agent-1")  # a 401-driven re-registration keeps the declared runner
    assert [b["runner"] for b in bodies] == ["docker", "docker"]
