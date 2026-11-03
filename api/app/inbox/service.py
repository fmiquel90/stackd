from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import RunState, RunType, TriggeredBy
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.user import User
from app.models.user_notification import UserNotification
from app.permissions import can_apply
from app.spaces.service import users_with_space_access

_MENTION_RE = re.compile(r"@([a-zA-Z0-9._+-]+)")


async def notify(
    session: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    *,
    run_id: uuid.UUID | None = None,
    comment_id: uuid.UUID | None = None,
    context: dict | None = None,
) -> None:
    """Add one in-app notification + emit a private WS signal on the user's channel (§17). Same txn
    as the trigger; NOTIFY is transactional so a rolled-back action never notifies."""
    session.add(
        UserNotification(
            user_id=user_id, kind=kind, run_id=run_id, comment_id=comment_id, context=context
        )
    )
    signal = json.dumps({"kind": "notification", "user_id": str(user_id)})
    await session.execute(
        text("SELECT pg_notify(:chan, :payload)").bindparams(chan=f"user_{user_id}", payload=signal)
    )


async def eligible_approver_ids(
    session: AsyncSession, env: Environment, *, is_destroy: bool = False
) -> list[uuid.UUID]:
    """Users who may confirm an apply on `env` — the rule `confirm_run` enforces, evaluated with
    the same `can_apply` (§2.4, §6 Phase F): space access (a member of the env's space, or an
    instance admin) AND the *effective* role/tiers (+ can_destroy for a destroy run) from that
    membership, else the instance defaults. So the fan-out neither leaks the env to a non-member
    nor misses a membership-only approver. The single who-may-approve fan-out — approval requests
    and drift alerts both use it. Fails closed: no stack → nobody; no allowed tier → not eligible.
    """
    stack = await session.get(Stack, env.stack_id)
    if stack is None:
        return []
    return [
        user.id
        for user, membership in await users_with_space_access(session, stack.space_id)
        if can_apply(user, env, membership, is_destroy=is_destroy).allowed
    ]


async def enqueue_for_transition(session: AsyncSession, run: Run, to_state: RunState) -> None:
    """Feed the in-app inbox off a run transition (called from transition(), §4.2):
    `unconfirmed` → fan out an approval request to every eligible approver; a terminal state →
    tell the human who triggered the run how it ended."""
    if to_state == RunState.unconfirmed:
        env = await session.get(Environment, run.environment_id)
        if env is None:
            return
        ctx = {"environment_id": str(env.id), "environment": env.name, "tier": env.tier}
        is_destroy = run.type == RunType.destroy  # as confirm_run: a destroy needs can_destroy
        for uid in await eligible_approver_ids(session, env, is_destroy=is_destroy):
            if uid == run.trigger_user_id:
                continue  # the triggerer already knows; avoids "approve your own" noise
            await notify(session, uid, "approval_request", run_id=run.id, context=ctx)
        return

    if to_state in (RunState.finished, RunState.failed) and run.trigger_user_id is not None:
        if run.triggered_by != TriggeredBy.manual:
            return
        kind = "run_finished" if to_state == RunState.finished else "run_failed"
        await notify(session, run.trigger_user_id, kind, run_id=run.id)


async def enqueue_for_comment(
    session: AsyncSession,
    run: Run,
    *,
    author_id: uuid.UUID,
    author_email: str,
    body: str,
    parent_author_id: uuid.UUID | None,
) -> None:
    """A new comment notifies the parent thread's author (a reply) and any @mentioned users
    (§16/§17), never the author themselves."""
    ctx = {"run_id": str(run.id)}
    notified: set[uuid.UUID] = {author_id}

    if parent_author_id is not None and parent_author_id not in notified:
        await notify(session, parent_author_id, "comment_reply", run_id=run.id, context=ctx)
        notified.add(parent_author_id)

    tokens = {m.lower() for m in _MENTION_RE.findall(body)}
    if not tokens:
        return
    # Match a mention token against the user's email local-part (before '@') or the full email.
    candidates = (await session.execute(select(User.id, User.email))).all()
    for uid, email in candidates:
        if uid in notified:
            continue
        local = email.split("@", 1)[0].lower()
        if local in tokens or email.lower() in tokens:
            await notify(
                session, uid, "mention", run_id=run.id, context={**ctx, "by": author_email}
            )
            notified.add(uid)
