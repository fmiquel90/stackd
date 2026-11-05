"""The worker's labels (SPECS §7.1/§7.2): `STACKD_WORKER_LABELS` (`key=value,…`), validated at
start, sent on every registration — the claim only hands a worker runs of environments whose labels
it carries."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import agent.main as main
from agent.client import ApiClient
from agent.config import Settings, parse_labels


def test_labels_parsed() -> None:
    assert parse_labels("tier=prod, region = eu-west-3,") == {
        "tier": "prod",
        "region": "eu-west-3",
    }
    assert parse_labels("") == {} and parse_labels(" , ") == {}
    assert parse_labels("gpu=") == {"gpu": ""}  # an empty value is a value
    assert parse_labels("url=a=b") == {"url": "a=b"}  # split on the first `=` only


@pytest.mark.parametrize("value", ["tier", "=prod", "tier=prod,region", "a=1,a=2"])
def test_bad_labels_refuse_to_start(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("STACKD_WORKER_LABELS", value)
    with pytest.raises(ValueError, match="STACKD_WORKER_LABELS"):
        Settings.from_env()


def test_labels_unset_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STACKD_WORKER_LABELS", raising=False)
    assert Settings.from_env().labels == {}


class _Stop(BaseException):
    """Ends `main.run()` right after its registration (the agent retries any `Exception`)."""


def test_agent_registers_with_its_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKD_POOL_TOKEN", "pool")
    monkeypatch.setenv("STACKD_RUNNER", "local")
    monkeypatch.setenv("STACKD_WORKER_LABELS", "tier=prod,region=eu-west-3")
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *_: Any) -> None:
            pass

        def register(self, pool_token: str, name: str, **kw: Any) -> str:
            calls.append(kw)
            raise _Stop

    monkeypatch.setattr(main, "ApiClient", _Client)
    monkeypatch.setattr(Settings, "available_tools", lambda self: {})
    with pytest.raises(_Stop):
        main.run()
    assert calls[0]["labels"] == {"tier": "prod", "region": "eu-west-3"}


def test_labels_kept_on_re_registration() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/worker/v1/register":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"worker_id": "w", "worker_token": "t"})
        # The worker token expired once: the client re-registers, then retries.
        return httpx.Response(401 if len(bodies) == 1 else 200, json={"commands": []})

    client = ApiClient("http://api")
    client._http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    client.register("pool", "agent-1", labels={"tier": "prod"}, runner="local")
    client.heartbeat()
    assert [b["labels"] for b in bodies] == [{"tier": "prod"}, {"tier": "prod"}]
