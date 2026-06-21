from __future__ import annotations

import asyncio
import uuid

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.user import User
from app.spaces import guard_env, guard_run


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


async def channel_for(sub: str, user: User, session: AsyncSession) -> str | None:
    """Map a subscription to a NOTIFY channel: run:<id> → run_<id>, environment:<id> → env_<id>,
    user:<id> → user_<id>. A `user:` channel is only granted for the connecting user's own id
    (the in-app notification feed is private), so one can't listen on someone else's signals.
    `run:`/`environment:` subscriptions are membership-gated the same way the REST routes are
    (§2/§6): the target must exist and the user must have at least reader access to its space,
    otherwise the subscription is denied (None)."""
    if sub.startswith("run:"):
        run = await _resolve(session, Run, sub[4:])
        if run is None:
            return None
        try:
            await guard_run(session, user, run)
        except ProblemException:
            return None
        return f"run_{run.id}"
    if sub.startswith("environment:"):
        env = await _resolve(session, Environment, sub[len("environment:") :])
        if env is None:
            return None
        try:
            await guard_env(session, user, env)
        except ProblemException:
            return None
        return f"env_{env.id}"
    if sub.startswith("user:"):
        return f"user_{user.id}" if sub[len("user:") :] == str(user.id) else None
    return None


async def _resolve(session: AsyncSession, model: type, raw_id: str):  # type: ignore[no-untyped-def]
    try:
        target_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    return await session.get(model, target_id)
