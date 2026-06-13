from __future__ import annotations

import asyncio

import asyncpg

from app.config import get_settings


class NotifyHub:
    """Postgres LISTEN/NOTIFY → WebSocket fan-out (SPECS §5.3).

    One asyncpg connection LISTENs per active channel; the payload is a light signal
    (`{kind, run_id, phase, max_seq}`) and clients re-read the source over REST. Works
    unchanged across multiple API replicas (each replica runs its own hub).
    """

    def __init__(self) -> None:
        self._conn: asyncpg.Connection | None = None
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def _ensure_conn(self) -> asyncpg.Connection:
        if self._conn is None or self._conn.is_closed():
            url = get_settings().database_url.replace("+asyncpg", "")
            self._conn = await asyncpg.connect(url)
        return self._conn

    def _on_notify(self, _conn, _pid, channel: str, payload: str) -> None:  # type: ignore[no-untyped-def]
        for q in self._subs.get(channel, set()):
            q.put_nowait(payload)

    async def subscribe(self, channel: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            conn = await self._ensure_conn()
            if channel not in self._subs:
                self._subs[channel] = set()
                await conn.add_listener(channel, self._on_notify)
            self._subs[channel].add(queue)
        return queue

    async def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subs.get(channel)
            if not subs:
                return
            subs.discard(queue)
            if not subs and self._conn is not None:
                await self._conn.remove_listener(channel, self._on_notify)
                del self._subs[channel]

    async def stop(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            await self._conn.close()


def channel_for(sub: str) -> str | None:
    """Map a subscription to a NOTIFY channel: run:<id> → run_<id>, environment:<id> → env_<id>."""
    if sub.startswith("run:"):
        return f"run_{sub[4:]}"
    if sub.startswith("environment:"):
        return f"env_{sub[len('environment:') :]}"
    return None
