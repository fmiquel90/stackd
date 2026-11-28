from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.user import User
from app.spaces import guard_env, guard_run

log = logging.getLogger(__name__)

# A subscriber queue holds `(channel, payload)` signals; `None` is the resync marker: signals may
# have been missed, re-read state.
SignalQueue = asyncio.Queue[tuple[str, str] | None]
RESYNC = None


def offer(queue: SignalQueue, item: tuple[str, str] | None) -> None:
    """Put without blocking, dropping the oldest pending item when full: signals are re-read hints,
    so a client that stops reading must not grow the process memory."""
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(item)


class NotifyHub:
    """Postgres LISTEN/NOTIFY → WebSocket fan-out (SPECS §5.3).

    One asyncpg connection per process LISTENs on every channel with a subscriber; the payload is a
    light signal (`{kind, run_id, phase, max_seq}`) and clients re-read the source over REST. Works
    unchanged across multiple API replicas (each replica runs its own hub).

    A supervisor task watches that connection — a termination listener for a clean loss, a periodic
    `SELECT 1` for a half-open one — and reopens it with a capped backoff, re-LISTENing on every
    subscribed channel. Signals sent while it was down are lost, so every subscriber receives
    `RESYNC` when the loss is detected and again once the connection is back.
    """

    def __init__(
        self,
        *,
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
        min_backoff: float = 0.5,
        max_backoff: float = 30.0,
    ) -> None:
        self._conn: asyncpg.Connection | None = None
        self._subs: dict[str, set[SignalQueue]] = {}
        self._lock = asyncio.Lock()
        self._lost = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff

    async def _connect(self) -> asyncpg.Connection:
        """Return the live connection, opening one if needed. Caller holds `_lock`."""
        if self._conn is not None and not self._conn.is_closed():
            return self._conn
        url = get_settings().database_url.replace("+asyncpg", "")
        conn = await asyncpg.connect(url, timeout=self._ping_timeout)
        for channel in self._subs:
            await self._bounded(conn, conn.add_listener(channel, self._on_notify))
        conn.add_termination_listener(self._on_terminated)
        self._conn = conn
        self._lost.clear()
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise())
        return conn

    async def _bounded(self, conn: asyncpg.Connection, op: Awaitable[object]) -> None:
        """Run a command on the LISTEN connection under a deadline: on a half-open connection it
        would otherwise hang forever while holding `_lock`. Failure terminates the connection."""
        try:
            await asyncio.wait_for(op, self._ping_timeout)
        except Exception:
            conn.terminate()
            raise

    def _on_terminated(self, conn: asyncpg.Connection) -> None:
        if conn is self._conn:
            self._lost.set()

    def _on_notify(self, _conn, _pid, channel: str, payload: str) -> None:  # type: ignore[no-untyped-def]
        for queue in self._subs.get(channel, ()):
            offer(queue, (channel, payload))

    def _resync_all(self) -> None:
        for queue in {q for subs in self._subs.values() for q in subs}:
            offer(queue, RESYNC)

    async def _supervise(self) -> None:
        while True:
            await self._wait_until_lost()
            log.warning("ws hub: LISTEN connection lost, reconnecting")
            self._resync_all()
            await self._reconnect()
            log.info("ws hub: LISTEN connection restored")
            self._resync_all()

    async def _wait_until_lost(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._lost.wait(), self._ping_interval)
                return
            except TimeoutError:
                pass
            async with self._lock:
                conn = self._conn
                if conn is None or conn.is_closed():
                    return
                try:
                    await self._bounded(conn, conn.fetchval("SELECT 1"))
                except Exception:
                    return

    async def _reconnect(self) -> None:
        delay = self._min_backoff
        while True:
            try:
                async with self._lock:
                    await self._connect()
                return
            except Exception as exc:
                log.warning("ws hub: reconnect failed (%s), retrying in %.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_backoff)

    async def subscribe(self, channel: str, queue: SignalQueue) -> None:
        async with self._lock:
            conn = await self._connect()
            subs = self._subs.get(channel)
            if subs is None:
                await self._bounded(conn, conn.add_listener(channel, self._on_notify))
                subs = self._subs[channel] = set()
            subs.add(queue)

    async def unsubscribe(self, channel: str, queue: SignalQueue) -> None:
        async with self._lock:
            subs = self._subs.get(channel)
            if subs is None:
                return
            subs.discard(queue)
            if subs:
                return
            del self._subs[channel]
            conn = self._conn
            if conn is not None:
                # A failed UNLISTEN terminates the connection: the supervisor replaces it, without
                # this channel.
                with contextlib.suppress(Exception):
                    await self._bounded(conn, conn.remove_listener(channel, self._on_notify))

    async def stop(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
        if self._conn is not None:
            self._conn.terminate()


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
    if sub.startswith("rollout:"):
        from app.models.rollout import EnvRollout
        from app.spaces import require_space_access

        rollout = await _resolve(session, EnvRollout, sub[len("rollout:") :])
        if rollout is None:
            return None
        try:
            await require_space_access(session, user, rollout.space_id)
        except ProblemException:
            return None
        return f"rollout_{rollout.id}"
    return None


async def _resolve(session: AsyncSession, model: type, raw_id: str):  # type: ignore[no-untyped-def]
    try:
        target_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    return await session.get(model, target_id)
