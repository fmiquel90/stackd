from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.enums import TERMINAL_STATES, AuditActorKind, RunEventActor, RunState
from app.errors import ProblemException
from app.logging import get_logger
from app.models.notification import NotificationOutbox
from app.models.run import Run, RunEvent
from app.models.vcs import VcsOutbox

_log = get_logger("stackd.runs")

# States that enqueue an outbound notification (the human-relevant ones): a decision is awaited,
# or the run reached a terminal outcome. Per-target filtering happens in the dispatcher.
NOTIFY_STATES: frozenset[RunState] = frozenset(
    {RunState.unconfirmed, RunState.finished, RunState.failed}
)

# States that post back to the VCS (Phase A / §18) for a PR-originated run: `planning` → a pending
# status, then the terminal outcome. Intermediate queued/preparing/checking are elided to avoid
# status churn. Only enqueued when the run carries `vcs_provider`.
VCS_POST_STATES: frozenset[RunState] = frozenset(
    {
        RunState.planning,
        RunState.finished,
        RunState.failed,
        RunState.canceled,
        RunState.discarded,
    }
)

# Legal state-machine edges (SPECS §4.1). `checking` is skipped when there are no after_plan hooks,
# so planning has the same outgoing edges as checking.
ALLOWED: dict[RunState, set[RunState]] = {
    RunState.queued: {RunState.preparing, RunState.canceled, RunState.discarded},
    # `running` is reached only by a RunType.command run (a one-off subcommand, no plan/apply).
    RunState.preparing: {RunState.planning, RunState.running, RunState.failed, RunState.canceled},
    RunState.running: {RunState.finished, RunState.failed, RunState.canceled},
    RunState.planning: {
        RunState.checking,
        RunState.unconfirmed,
        RunState.confirmed,
        RunState.finished,
        RunState.failed,
        RunState.canceled,
    },
    RunState.checking: {
        RunState.unconfirmed,
        RunState.confirmed,
        RunState.finished,
        RunState.failed,
        RunState.canceled,
    },
    RunState.unconfirmed: {
        RunState.confirmed,
        RunState.discarded,
        RunState.canceled,
        RunState.failed,
    },
    RunState.confirmed: {RunState.applying, RunState.failed, RunState.canceled},
    RunState.applying: {RunState.finished, RunState.failed},
}

_ACTOR_KIND = {
    RunEventActor.user: AuditActorKind.user,
    RunEventActor.worker: AuditActorKind.worker,
    RunEventActor.system: AuditActorKind.system,
}


async def transition(
    session: AsyncSession,
    run: Run,
    to_state: RunState,
    *,
    actor: RunEventActor,
    actor_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    payload: dict | None = None,
    fields: dict[str, Any] | None = None,
    audit_action: str | None = None,
    audit_context: dict | None = None,
) -> None:
    """The ONLY way a run's state changes (CLAUDE invariant #1, SPECS §4.2).

    Verifies legality, performs an atomic guarded update on `from_state` (row lock + state check),
    writes the run_event, writes an audit_event in the SAME transaction when `audit_action` is
    given (mutating/human/terminal actions), and emits a LISTEN/NOTIFY signal for WS fan-out
    (§5.3). Flushes so a one_active_run_per_env violation (23505) surfaces to the caller.
    Does not commit — the caller owns the transaction.
    """
    from_state = run.state
    if to_state not in ALLOWED.get(from_state, set()):
        raise ProblemException(
            409, "Illegal transition", f"{from_state.value} → {to_state.value} is not allowed."
        )

    # Guarded, atomic: lock the row and re-check the state we believe we are leaving.
    locked = (
        await session.execute(
            select(Run).where(Run.id == run.id, Run.state == from_state).with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        raise ProblemException(409, "Concurrent modification", "Run state changed underneath.")

    locked.state = to_state
    for key, value in (fields or {}).items():
        setattr(locked, key, value)
    if to_state in TERMINAL_STATES and locked.finished_at is None:
        locked.finished_at = datetime.now(UTC)
        # Run duration histogram (§H): claim → terminal, labeled by job phase (a run with a
        # confirmation went through apply, otherwise it was plan-only).
        if locked.claimed_at is not None:
            from app.observability import metrics

            phase = "apply" if locked.confirmed_at is not None else "plan"
            metrics.run_duration.labels(phase=phase).observe(
                (locked.finished_at - locked.claimed_at).total_seconds()
            )

    session.add(
        RunEvent(
            run_id=run.id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            actor_id=actor_id,
            payload=payload,
        )
    )

    if audit_action is not None:
        await record_audit(
            session,
            action=audit_action,
            actor_kind=_ACTOR_KIND[actor],
            actor_id=actor_id,
            actor_email=actor_email,
            target_kind="run",
            target_id=run.id,
            context={"from": from_state.value, "to": to_state.value, **(audit_context or {})},
        )

    # Transactional outbox: enqueue an outbound notification in the SAME txn (no I/O here);
    # the scheduler drains it after commit, so a rolled-back transition never notifies.
    if to_state in NOTIFY_STATES:
        session.add(NotificationOutbox(run_id=run.id, to_state=to_state.value))

    # VCS post-back outbox (§18): only for PR-originated runs; drained by the scheduler post-commit.
    if locked.vcs_provider and to_state in VCS_POST_STATES:
        session.add(VcsOutbox(run_id=run.id, to_state=to_state.value))

    # In-app notification center (§17): fan out to approvers / the triggerer, same txn.
    from app.inbox.service import enqueue_for_transition

    await enqueue_for_transition(session, run, to_state)

    # Light WS fan-out signal in the same transaction (§5.3) — content is re-read by replicas.
    signal = json.dumps({"kind": "run_event", "run_id": str(run.id), "to_state": to_state.value})
    await session.execute(
        text("SELECT pg_notify(:chan, :payload)").bindparams(chan=f"run_{run.id}", payload=signal)
    )
    await session.execute(
        text("SELECT pg_notify(:chan, :payload)").bindparams(
            chan=f"env_{run.environment_id}", payload=signal
        )
    )

    await session.flush()
    _log.log(
        logging.WARNING if to_state == RunState.failed else logging.INFO,
        "run transition",
        extra={
            "event": "run.transition",
            "run_id": str(run.id),
            "environment_id": str(run.environment_id),
            "from": from_state.value,
            "to": to_state.value,
            "actor": actor.value,
            "actor_id": str(actor_id) if actor_id else None,
        },
    )
