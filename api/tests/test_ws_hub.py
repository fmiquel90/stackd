"""WS hub resilience (SPECS §5.3): the LISTEN connection is supervised and reopened, queues are
bounded, a socket's subscriptions are deduplicated and capped, and auth must arrive in time."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable

import asyncpg
import httpx
import pytest
from sqlalchemy.engine import make_url
from starlette.websockets import WebSocket

from app.config import get_settings
from app.security import decode_access_token
from app.ws import router
from app.ws.hub import RESYNC, NotifyHub, SignalQueue, offer
from tests.conftest_phase2 import login, make_env, make_stack


def _dsn() -> str:
    return os.environ["DATABASE_URL"].replace("+asyncpg", "")


async def _notify(channel: str, payload: str) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("SELECT pg_notify($1, $2)", channel, payload)
    finally:
        await conn.close()


async def _eventually(check: Callable[[], bool]) -> None:
    """Poll hub state that has no event to wait on."""
    async with asyncio.timeout(10):
        while True:
            if check():
                return
            await asyncio.sleep(0.05)


async def _await_reconnect(hub: NotifyHub, old_pid: int) -> None:
    def reconnected() -> bool:
        conn = hub._conn
        return conn is not None and not conn.is_closed() and conn.get_server_pid() != old_pid

    await _eventually(reconnected)


async def _await_signal(queue: SignalQueue, channel: str) -> None:
    """NOTIFY until the queue gets it: the hub may still be re-LISTENing right after reconnect."""
    async with asyncio.timeout(10):
        while True:
            await _notify(channel, '{"kind": "run_event"}')
            await asyncio.sleep(0.1)
            while not queue.empty():
                if queue.get_nowait() == '{"kind": "run_event"}':
                    return


@pytest.fixture
async def hub(_database: str) -> AsyncIterator[NotifyHub]:
    h = NotifyHub(ping_interval=0.2, ping_timeout=0.5, min_backoff=0.05, max_backoff=0.2)
    yield h
    await h.stop()


async def test_hub_resumes_after_listen_connection_is_killed(hub: NotifyHub) -> None:
    channel = f"run_{uuid.uuid4()}"
    queue: SignalQueue = asyncio.Queue(maxsize=16)
    await hub.subscribe(channel, queue)
    await _await_signal(queue, channel)

    assert hub._conn is not None
    old_pid = hub._conn.get_server_pid()
    admin = await asyncpg.connect(_dsn())
    try:
        await admin.execute("SELECT pg_terminate_backend($1)", old_pid)
    finally:
        await admin.close()

    async with asyncio.timeout(10):
        assert await queue.get() is RESYNC
    await _await_reconnect(hub, old_pid)
    # The existing subscription is listened on again, without anyone re-subscribing.
    await _await_signal(queue, channel)

    # A newcomer on the already-known channel shares that restored listener.
    late: SignalQueue = asyncio.Queue(maxsize=16)
    await hub.subscribe(channel, late)
    await _await_signal(late, channel)


class _FreezableProxy:
    """TCP relay to Postgres whose live connections can be frozen (bytes silently dropped) to mimic
    a half-open connection: nothing errors, the peer just stops answering."""

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._live: list[asyncio.Event] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    def freeze(self) -> None:
        for frozen in self._live:
            frozen.set()
        self._live = []

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection(*self._target)
        frozen = asyncio.Event()
        self._live.append(frozen)

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(OSError):
                while data := await src.read(65536):
                    if not frozen.is_set():
                        dst.write(data)
                        await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()


async def test_hub_replaces_a_half_open_connection(
    hub: NotifyHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(os.environ["DATABASE_URL"])
    proxy = _FreezableProxy(url.host or "localhost", url.port or 5432)
    port = await proxy.start()
    monkeypatch.setattr(
        get_settings(),
        "database_url",
        url.set(host="127.0.0.1", port=port).render_as_string(hide_password=False),
    )
    try:
        channel = f"env_{uuid.uuid4()}"
        queue: SignalQueue = asyncio.Queue(maxsize=16)
        await hub.subscribe(channel, queue)
        await _await_signal(queue, channel)
        assert hub._conn is not None
        old_pid = hub._conn.get_server_pid()

        proxy.freeze()

        async with asyncio.timeout(10):
            assert await queue.get() is RESYNC
        await _await_reconnect(hub, old_pid)
        await _await_signal(queue, channel)
    finally:
        await hub.stop()
        await proxy.stop()


async def test_subscriber_queue_is_bounded_and_keeps_the_newest(hub: NotifyHub) -> None:
    channel = f"run_{uuid.uuid4()}"
    queue: SignalQueue = asyncio.Queue(maxsize=2)
    await hub.subscribe(channel, queue)
    for i in range(5):
        await _notify(channel, str(i))
    await _eventually(queue.full)
    await asyncio.sleep(0.2)  # the later signals keep evicting the oldest
    assert [queue.get_nowait(), queue.get_nowait()] == ["3", "4"]


def test_offer_drops_the_oldest_when_full() -> None:
    queue: SignalQueue = asyncio.Queue(maxsize=2)
    for item in ("a", "b", "c"):
        offer(queue, item)
    assert [queue.get_nowait(), queue.get_nowait()] == ["b", "c"]


class _Socket:
    """Drives `ws_endpoint` over raw ASGI messages on the test's own loop (the DB engine lives on
    it; a TestClient would run the endpoint on another loop)."""

    def __init__(self) -> None:
        self._in: asyncio.Queue[dict] = asyncio.Queue()
        self._out: asyncio.Queue[dict] = asyncio.Queue()
        self._in.put_nowait({"type": "websocket.connect"})
        scope = {"type": "websocket", "path": "/api/v1/ws", "headers": [], "query_string": b""}
        ws = WebSocket(scope, self._in.get, self._out.put)
        self.task = asyncio.create_task(router.ws_endpoint(ws))

    def send(self, obj: object) -> None:
        self._in.put_nowait({"type": "websocket.receive", "text": json.dumps(obj)})

    async def next_message(self, within: float = 10.0) -> dict:
        async with asyncio.timeout(within):
            while (msg := await self._out.get())["type"] == "websocket.accept":
                pass
        return msg

    async def close_code(self) -> int:
        msg = await self.next_message()
        assert msg["type"] == "websocket.close", msg
        await asyncio.wait_for(self.task, 10)
        return int(msg["code"])

    async def disconnect(self) -> None:
        self._in.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 10)


@pytest.fixture
async def ws_hub(hub: NotifyHub, monkeypatch: pytest.MonkeyPatch) -> NotifyHub:
    monkeypatch.setattr(router, "_hub", hub)
    return hub


async def _token(client: httpx.AsyncClient) -> tuple[str, str]:
    token = (await login(client, "admin"))["Authorization"].removeprefix("Bearer ")
    return token, str(decode_access_token(token)["sub"])


async def test_auth_message_must_arrive_in_time(
    _database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "stackd_ws_auth_timeout_seconds", 0.2)
    sock = _Socket()
    assert await sock.close_code() == 4401


async def test_repeated_subscription_is_deduplicated(
    client: httpx.AsyncClient, ws_hub: NotifyHub
) -> None:
    token, user_id = await _token(client)
    channel = f"user_{user_id}"
    sock = _Socket()
    sock.send({"type": "auth", "token": token})
    for _ in range(3):
        sock.send({"sub": f"user:{user_id}"})
    await _eventually(lambda: channel in ws_hub._subs)
    await asyncio.sleep(0.2)  # let the repeats be handled
    assert len(ws_hub._subs[channel]) == 1

    await _notify(channel, '{"kind": "notification"}')
    assert (await sock.next_message())["text"] == '{"kind": "notification"}'
    with pytest.raises(TimeoutError):
        await sock.next_message(within=0.3)  # one subscription → one copy

    await sock.disconnect()
    assert channel not in ws_hub._subs


async def test_subscriptions_are_capped(
    client: httpx.AsyncClient, ws_hub: NotifyHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "stackd_ws_max_subscriptions", 1)
    token, user_id = await _token(client)
    admin = await login(client, "admin")
    env_id = await make_env(client, admin, await make_stack(client, admin, "ws-cap"), "dev", "dev")

    sock = _Socket()
    sock.send({"type": "auth", "token": token})
    sock.send({"sub": f"user:{user_id}"})
    sock.send({"sub": f"user:{user_id}"})  # a repeat does not count against the cap
    sock.send({"sub": f"environment:{env_id}"})
    assert await sock.close_code() == 1008
    assert ws_hub._subs == {}


async def test_lost_listen_connection_closes_sockets_for_a_resync(
    client: httpx.AsyncClient, ws_hub: NotifyHub
) -> None:
    token, user_id = await _token(client)
    sock = _Socket()
    sock.send({"type": "auth", "token": token})
    sock.send({"sub": f"user:{user_id}"})
    await _eventually(lambda: f"user_{user_id}" in ws_hub._subs)
    assert ws_hub._conn is not None

    admin = await asyncpg.connect(_dsn())
    try:
        await admin.execute("SELECT pg_terminate_backend($1)", ws_hub._conn.get_server_pid())
    finally:
        await admin.close()

    assert await sock.close_code() == 1012
    assert ws_hub._subs == {}
