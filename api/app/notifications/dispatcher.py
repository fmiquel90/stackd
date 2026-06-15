from __future__ import annotations

import uuid
from datetime import datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import AttachmentTarget
from app.logging import get_logger
from app.models.environment import Environment
from app.models.notification import NotificationOutbox, NotificationTarget
from app.models.run import Run
from app.models.stack import Stack

_log = get_logger("stackd.notifications")

_MAX_ATTEMPTS = 5  # after this, the row is dead-lettered (left unsent, excluded from the poll)
_HTTP_TIMEOUT = 5.0

# Short human label per state for the message subject line.
_LABEL = {
    "unconfirmed": "plan awaiting confirmation",
    "finished": "run finished",
    "failed": "run failed",
}


def _run_url(run_id: str) -> str | None:
    settings = get_settings()
    base = settings.stackd_app_url or settings.stackd_public_url
    return f"{base.rstrip('/')}/runs/{run_id}" if base else None


def _render(
    target: NotificationTarget, to_state: str, run: Run, stack: Stack, env: Environment
) -> dict:
    label = _LABEL.get(to_state, to_state)
    scope = f"{stack.name}/{env.name}"
    url = _run_url(str(run.id))
    if target.kind.value == "slack":
        line = f"*{scope}* — {label} ({to_state})"
        if url:
            line += f"\n<{url}|Open run>"
        return {"text": line}
    # Generic webhook envelope.
    return {
        "event": "run.state_changed",
        "state": to_state,
        "run_id": str(run.id),
        "run_type": run.type.value,
        "stack": stack.name,
        "environment": env.name,
        "tier": env.tier,
        "commit_sha": run.commit_sha,
        "url": url,
    }


async def _matching_targets(
    session: AsyncSession, stack: Stack, env: Environment, to_state: str
) -> list[NotificationTarget]:
    rows = (
        (
            await session.execute(
                select(NotificationTarget).where(
                    NotificationTarget.enabled.is_(True),
                    (
                        (NotificationTarget.target_kind == AttachmentTarget.environment)
                        & (NotificationTarget.target_id == env.id)
                    )
                    | (
                        (NotificationTarget.target_kind == AttachmentTarget.stack)
                        & (NotificationTarget.target_id == stack.id)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return [t for t in rows if to_state in (t.on_states or [])]


async def _deliver(target: NotificationTarget, body: dict) -> bool:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
        resp = await http.post(target.url, json=body)
        resp.raise_for_status()
    return True


async def dispatch_pending(session: AsyncSession, now: datetime, *, limit: int = 20) -> int:
    """Drain the notification outbox without holding a DB transaction across external HTTP I/O.

    Phase 1 (locked, brief): claim a batch with FOR UPDATE SKIP LOCKED, bump `attempts`, commit —
    this releases the row locks immediately. Phase 2 (no DB txn held): POST to matching targets.
    Phase 3: mark the delivered rows `sent_at`. A crash between phases leaves `attempts` bumped
    and `sent_at` null → retried later (at-least-once); the advisory lock keeps it single."""
    rows = (
        (
            await session.execute(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.sent_at.is_(None),
                    NotificationOutbox.attempts < _MAX_ATTEMPTS,
                )
                .order_by(NotificationOutbox.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    # Phase 1: snapshot what we need, bump attempts, release the locks.
    batch = [(r.id, r.run_id, r.to_state, r.attempts + 1) for r in rows]
    for r in rows:
        r.attempts += 1
    await session.commit()

    # Phase 2: deliver with no DB transaction open.
    delivered: list[uuid.UUID] = []
    for row_id, run_id, to_state, attempt in batch:
        run = await session.get(Run, run_id)
        env = await session.get(Environment, run.environment_id) if run else None
        stack = await session.get(Stack, env.stack_id) if env else None
        if run is None or env is None or stack is None:
            delivered.append(row_id)  # nothing to notify — mark done
            continue
        targets = await _matching_targets(session, stack, env, to_state)
        ok = True
        for target in targets:
            try:
                await _deliver(target, _render(target, to_state, run, stack, env))
            except Exception as exc:  # external endpoint — never crash the loop
                ok = False
                _log.warning(
                    "notification delivery failed",
                    extra={
                        "event": "notification.failed",
                        "target_id": str(target.id),
                        "run_id": str(run_id),
                        "state": to_state,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
        if ok:
            delivered.append(row_id)
            if targets:
                _log.info(
                    "notifications delivered",
                    extra={
                        "event": "notification.delivered",
                        "run_id": str(run_id),
                        "state": to_state,
                        "targets": len(targets),
                    },
                )

    # Phase 3: mark the delivered rows done.
    if delivered:
        await session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id.in_(delivered))
            .values(sent_at=now)
        )
        await session.commit()
    return len(batch)
