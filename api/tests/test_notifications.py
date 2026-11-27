from __future__ import annotations

import ipaddress
import socket
import ssl
from datetime import UTC, datetime

import httpcore
import httpx
import pytest

from tests.conftest_phase2 import event, login, make_env, make_stack, register_worker

CHANGES = {"has_changes": True, "summary": {"add": 1, "change": 0, "destroy": 0}}


async def _drive_to_unconfirmed(client, wh, env_id, admin) -> str:
    run = (await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})).json()
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    await event(client, wh, run["id"], "phase_started", phase="planning")
    await event(client, wh, run["id"], "phase_finished", result=CHANGES)
    return run["id"]


async def test_notification_target_crud(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "notif-crud")

    created = await client.post(
        f"/api/v1/stacks/{stack}/notifications",
        headers=admin,
        json={"name": "ops-slack", "kind": "slack", "url": "https://hooks.example/x"},
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    assert created.json()["on_states"] == ["unconfirmed", "failed"]  # default

    listed = await client.get(f"/api/v1/stacks/{stack}/notifications", headers=admin)
    assert [t["id"] for t in listed.json()] == [tid]

    patched = await client.patch(
        f"/api/v1/stacks/{stack}/notifications/{tid}", headers=admin, json={"enabled": False}
    )
    assert patched.json()["enabled"] is False

    deleted = await client.delete(f"/api/v1/stacks/{stack}/notifications/{tid}", headers=admin)
    assert deleted.status_code == 204


async def test_send_test_notification(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.notifications import router as notif_router

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "notif-test")
    tid = (
        await client.post(
            f"/api/v1/stacks/{stack}/notifications",
            headers=admin,
            json={"name": "ops", "kind": "slack", "url": "https://hooks.example/x"},
        )
    ).json()["id"]

    captured: list[dict] = []

    async def fake_deliver(target, body):  # type: ignore[no-untyped-def]
        captured.append(body)
        return True

    monkeypatch.setattr(notif_router, "deliver", fake_deliver)
    r = await client.post(f"/api/v1/stacks/{stack}/notifications/{tid}/test", headers=admin)
    assert r.status_code == 200 and r.json()["ok"] is True
    # The payload is explicitly flagged as a test (slack → "test" in the text).
    assert "test" in captured[0]["text"].lower()

    audit = await client.get(
        "/api/v1/audit", headers=admin, params={"target_kind": "notification_target"}
    )
    actions = {e["action"] for e in audit.json()}
    assert {
        "notification_target.created",
        "notification_target.updated",
        "notification_target.deleted",
    } <= actions


async def test_rejects_unsupported_state(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "notif-badstate")
    r = await client.post(
        f"/api/v1/stacks/{stack}/notifications",
        headers=admin,
        json={"name": "x", "kind": "webhook", "url": "https://e/x", "on_states": ["planning"]},
    )
    assert r.status_code == 422


async def test_outbox_enqueued_on_transition(client: httpx.AsyncClient) -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.notification import NotificationOutbox

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "notif-pool-1")
    stack = await make_stack(client, admin, "notif-enqueue")
    env = await make_env(client, admin, stack, "dev", "dev")

    run_id = await _drive_to_unconfirmed(client, wh, env, admin)

    async with SessionLocal() as s:
        import uuid

        rows = (
            (
                await s.execute(
                    select(NotificationOutbox).where(NotificationOutbox.run_id == uuid.UUID(run_id))
                )
            )
            .scalars()
            .all()
        )
    assert [r.to_state for r in rows] == ["unconfirmed"]


async def test_dispatcher_delivers_to_matching_targets(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import SessionLocal
    from app.notifications import dispatcher

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "notif-pool-2")
    stack = await make_stack(client, admin, "notif-dispatch")
    env = await make_env(client, admin, stack, "dev", "dev")

    # One target fires on unconfirmed (should deliver), one only on failed (should NOT).
    fire = await client.post(
        f"/api/v1/environments/{env}/notifications",
        headers=admin,
        json={
            "name": "fires",
            "kind": "webhook",
            "url": "https://e/fire",
            "on_states": ["unconfirmed"],
        },
    )
    assert fire.status_code == 201
    await client.post(
        f"/api/v1/environments/{env}/notifications",
        headers=admin,
        json={"name": "quiet", "kind": "slack", "url": "https://e/quiet", "on_states": ["failed"]},
    )

    captured: list[tuple[str, dict]] = []

    async def fake_deliver(target, body):  # type: ignore[no-untyped-def]
        captured.append((target.url, body))
        return True

    monkeypatch.setattr(dispatcher, "deliver", fake_deliver)

    run_id = await _drive_to_unconfirmed(client, wh, env, admin)

    async with SessionLocal() as s:
        processed = await dispatcher.dispatch_pending(s, datetime.now(UTC))
    assert processed == 1
    assert [url for url, _ in captured] == ["https://e/fire"]
    body = captured[0][1]
    assert body["state"] == "unconfirmed"
    assert body["run_id"] == run_id
    assert body["environment"] == "dev"

    # Second drain is a no-op: the row is marked sent.
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_pending(s, datetime.now(UTC)) == 0
    assert len(captured) == 1


def _throttled(retry_after: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://e/flaky")
    response = httpx.Response(429, headers={"Retry-After": retry_after}, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


async def _outbox_row(run_id: str):  # type: ignore[no-untyped-def]
    import uuid

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.notification import NotificationOutbox

    async with SessionLocal() as s:
        q = select(NotificationOutbox).where(NotificationOutbox.run_id == uuid.UUID(run_id))
        return (await s.execute(q)).scalar_one()


async def test_dispatcher_retries_only_failed_targets_after_backoff(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing target is retried once due — not before its Retry-After — and alone: the targets
    already delivered don't get the message again. No DB transaction is open during a POST."""
    from datetime import timedelta

    from app.db import SessionLocal
    from app.notifications import dispatcher

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "notif-pool-retry")
    stack = await make_stack(client, admin, "notif-retry")
    env = await make_env(client, admin, stack, "dev", "dev")
    for name in ("steady", "flaky"):
        r = await client.post(
            f"/api/v1/environments/{env}/notifications",
            headers=admin,
            json={"name": name, "kind": "webhook", "url": f"https://e/{name}"},
        )
        assert r.status_code == 201

    posted: list[str] = []
    txn_open: list[bool] = []
    flaky_failures = [_throttled("120")]

    async def fake_deliver(target, body):  # type: ignore[no-untyped-def]
        txn_open.append(s.in_transaction())
        if target.url == "https://e/flaky" and flaky_failures:
            raise flaky_failures.pop()
        posted.append(target.url)
        return True

    monkeypatch.setattr(dispatcher, "deliver", fake_deliver)
    run_id = await _drive_to_unconfirmed(client, wh, env, admin)

    now = datetime.now(UTC)
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_pending(s, now) == 1
    assert posted == ["https://e/steady"]
    row = await _outbox_row(run_id)
    assert row.sent_at is None and row.attempts == 1
    assert len(row.delivered_target_ids) == 1
    assert row.next_attempt_at >= now + timedelta(seconds=120)  # the receiver's Retry-After

    for later in (now, now + timedelta(seconds=60)):  # not due yet
        async with SessionLocal() as s:
            assert await dispatcher.dispatch_pending(s, later) == 0

    async with SessionLocal() as s:
        assert await dispatcher.dispatch_pending(s, now + timedelta(seconds=180)) == 1
    assert posted == ["https://e/steady", "https://e/flaky"]  # steady was not re-sent
    row = await _outbox_row(run_id)
    assert row.sent_at is not None and row.attempts == 2
    assert txn_open and not any(txn_open)


async def test_dispatcher_reports_a_row_out_of_attempts(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from datetime import timedelta

    from prometheus_client import REGISTRY

    from app.config import get_settings
    from app.db import SessionLocal
    from app.notifications import dispatcher

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, "notif-pool-dead")
    stack = await make_stack(client, admin, "notif-dead")
    env = await make_env(client, admin, stack, "dev", "dev")
    tid = (
        await client.post(
            f"/api/v1/environments/{env}/notifications",
            headers=admin,
            json={"name": "down", "kind": "webhook", "url": "https://e/down"},
        )
    ).json()["id"]

    async def failing_deliver(target, body):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(dispatcher, "deliver", failing_deliver)
    run_id = await _drive_to_unconfirmed(client, wh, env, admin)

    max_attempts = get_settings().stackd_outbox_max_attempts
    labels = {"outbox": "notification"}
    dead_before = REGISTRY.get_sample_value("stackd_outbox_dead_letters_total", labels) or 0.0
    later = datetime.now(UTC)
    for _ in range(max_attempts):
        async with SessionLocal() as s:
            assert await dispatcher.dispatch_pending(s, later) == 1
        later += timedelta(hours=2)
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_pending(s, later) == 0
    row = await _outbox_row(run_id)
    assert row.sent_at is None and row.attempts == max_attempts

    dead = [r for r in caplog.records if getattr(r, "event", None) == "notification.dead_lettered"]
    assert [(r.levelname, r.run_id, r.failed_target_ids) for r in dead] == [
        ("ERROR", run_id, [tid])
    ]
    assert REGISTRY.get_sample_value("stackd_outbox_dead_letters_total", labels) == dead_before + 1


# --- SSRF guard: DNS is resolved once, off the event loop, and the POST is pinned to that answer ---

_PUBLIC_IP = "93.184.216.34"
_real_getaddrinfo = socket.getaddrinfo


def _fake_dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str]]) -> list[str]:
    """Each lookup of a host in `answers` returns its next IP (the last one sticks), so
    `[public, private]` is a TTL-0 rebinding record. Other hosts resolve normally (the DB). Returns
    the log of scripted lookups."""
    lookups: list[str] = []

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host not in answers:
            return _real_getaddrinfo(host, *args, **kwargs)
        lookups.append(host)
        ip = answers[host].pop(0) if len(answers[host]) > 1 else answers[host][0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return lookups


class _FakeStream(httpcore.AsyncNetworkStream):
    """Serves one canned HTTP response; records the TLS parameters and the request bytes.
    (httpcore passes `timeout` by keyword only; the fakes swallow it via `**_`.)"""

    def __init__(self, net: dict, response: bytes) -> None:
        self._net, self._response = net, response

    async def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, **_: object
    ) -> httpcore.AsyncNetworkStream:
        self._net["tls"] = (server_hostname, ssl_context.check_hostname, ssl_context.verify_mode)
        return self

    async def write(self, buffer: bytes, **_: object) -> None:
        self._net["sent"] += buffer

    async def read(self, max_bytes: int, **_: object) -> bytes:
        chunk, self._response = self._response, b""
        return chunk

    async def aclose(self) -> None:
        pass


def _fake_net(
    monkeypatch: pytest.MonkeyPatch, response: bytes = b"HTTP/1.1 204 No Content\r\n\r\n"
) -> dict:
    """Stand in for httpcore's TCP connect. Like the real backend, a hostname that reaches it is
    resolved (again) — that second lookup is the rebinding window. Records the address dialled."""
    net: dict = {"connected": [], "tls": None, "sent": b""}

    async def connect_tcp(self, host, port, **_):  # type: ignore[no-untyped-def]
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = socket.getaddrinfo(host, port)[0][4][0]
        net["connected"].append((ip, port))
        return _FakeStream(net, response)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    # An env proxy would resolve the hostname on its own side; delivery must not go through it.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    return net


async def test_deliver_pins_vetted_ip_against_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.notification import NotificationTarget
    from app.notifications.dispatcher import deliver

    # The check's lookup answers a public IP; any later lookup answers the metadata endpoint.
    lookups = _fake_dns(monkeypatch, {"rebind.example": [_PUBLIC_IP, "169.254.169.254"]})
    net = _fake_net(monkeypatch)

    assert await deliver(NotificationTarget(url="https://rebind.example/hook?x=1"), {"a": 1})

    assert lookups == ["rebind.example"]  # resolved exactly once
    assert net["connected"] == [(_PUBLIC_IP, 443)]  # the vetted IP, never the rebound one
    # TLS still authenticates the original hostname: SNI + certificate/hostname verification.
    assert net["tls"] == ("rebind.example", True, ssl.CERT_REQUIRED)
    assert net["sent"].startswith(b"POST /hook?x=1 HTTP/1.1\r\n")
    assert b"\r\nhost: rebind.example\r\n" in net["sent"].lower()


async def test_deliver_refuses_host_resolving_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.notification import NotificationTarget
    from app.notifications.dispatcher import deliver

    _fake_dns(monkeypatch, {"internal.example": ["10.0.0.7"]})
    net = _fake_net(monkeypatch)
    with pytest.raises(ValueError, match=r"non-public address \(10\.0\.0\.7\)"):
        await deliver(NotificationTarget(url="https://internal.example/hook"), {})
    assert net["connected"] == []


async def test_deliver_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models.notification import NotificationTarget
    from app.notifications.dispatcher import deliver

    _fake_dns(monkeypatch, {"hooks.example": [_PUBLIC_IP]})
    net = _fake_net(
        monkeypatch,
        b"HTTP/1.1 302 Found\r\nLocation: https://169.254.169.254/\r\nContent-Length: 0\r\n\r\n",
    )
    with pytest.raises(httpx.HTTPStatusError):
        await deliver(NotificationTarget(url="https://hooks.example/hook"), {})
    assert net["connected"] == [(_PUBLIC_IP, 443)]


async def test_rejects_url_resolving_to_private_ip(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_dns(monkeypatch, {"internal.example": ["10.0.0.7"], "hooks.example": [_PUBLIC_IP]})
    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "notif-ssrf")
    base = f"/api/v1/stacks/{stack}/notifications"
    body = {"name": "x", "kind": "webhook", "url": "https://internal.example/x"}

    r = await client.post(base, headers=admin, json=body)
    assert r.status_code == 422, r.text
    assert r.headers["content-type"] == "application/problem+json"
    [err] = r.json()["errors"]
    assert err["loc"] == ["body", "url"]
    assert err["msg"] == (
        "Value error, url host 'internal.example' resolves to a non-public address (10.0.0.7)"
    )

    ok = await client.post(base, headers=admin, json={**body, "url": "https://hooks.example/x"})
    assert ok.status_code == 201, ok.text
    r = await client.patch(
        f"{base}/{ok.json()['id']}", headers=admin, json={"url": "https://internal.example/y"}
    )
    assert r.status_code == 422, r.text
    assert r.json()["errors"][0]["loc"] == ["body", "url"]
    listed = await client.get(base, headers=admin)
    assert [t["url"] for t in listed.json()] == ["https://hooks.example/x"]  # unchanged


def test_schema_validation_does_not_resolve_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from app.notifications.schemas import NotificationCreate, NotificationUpdate

    lookups = _fake_dns(monkeypatch, {"hooks.example": ["10.0.0.7"]})
    NotificationCreate(name="x", kind="webhook", url="https://hooks.example/x")
    NotificationUpdate(url="https://hooks.example/y")
    assert lookups == []  # resolution is the routes' (async) job, never the validators'

    # The cheap, DNS-free checks still run at parse time.
    for url in ["http://hooks.example/x", "https://169.254.169.254/latest", "https://[::1]/x"]:
        with pytest.raises(ValidationError):
            NotificationCreate(name="x", kind="webhook", url=url)
