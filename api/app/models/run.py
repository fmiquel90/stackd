from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, created_at_col, pk_uuid
from app.enums import RunEventActor, RunState, RunType, TriggeredBy


class Run(Base):
    """A run belongs to an environment (SPECS §3.5). State changes ONLY via transition() (§4.2)."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = pk_uuid()
    environment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("environments.id", ondelete="CASCADE")
    )
    type: Mapped[RunType] = mapped_column(Enum(RunType, name="run_type"), default=RunType.tracked)
    state: Mapped[RunState] = mapped_column(
        Enum(RunState, name="run_state"), default=RunState.queued
    )

    commit_sha: Mapped[str | None] = mapped_column(String, default=None)
    commit_message: Mapped[str | None] = mapped_column(String, default=None)
    commit_author: Mapped[str | None] = mapped_column(String, default=None)

    # VCS feedback (§18/Phase A): set on a `proposed` run spawned by a PR. The post-back (commit
    # status + one edited PR comment) is driven off `transition()` via the vcs_outbox.
    pr_number: Mapped[int | None] = mapped_column(Integer, default=None)
    vcs_provider: Mapped[str | None] = mapped_column(String, default=None)  # 'github'
    vcs_comment_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    vcs_head_sha: Mapped[str | None] = mapped_column(String, default=None)

    triggered_by: Mapped[TriggeredBy] = mapped_column(Enum(TriggeredBy, name="triggered_by"))
    trigger_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), default=None
    )

    # A scheduler-spawned read-only drift plan (§19): proposed + `-refresh-only`, queued behind user
    # runs in the claim, routes to a drift-status update instead of a normal proposed outcome.
    is_drift: Mapped[bool] = mapped_column(Boolean, default=False)

    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    run_group_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)

    # For RunType.command: {"name": "<subcommand>", "args": [...]} (SPECS §4.3).
    command: Mapped[dict | None] = mapped_column(JSONB, default=None)

    plan_summary: Mapped[dict | None] = mapped_column(JSONB, default=None)
    check_results: Mapped[dict | None] = mapped_column(JSONB, default=None)
    resolved_inputs: Mapped[dict | None] = mapped_column(JSONB, default=None)
    used_mocks: Mapped[bool] = mapped_column(Boolean, default=False)
    # A secret reference resolved via fallback (static value or break-glass override) — blocks apply
    # unless environment.allow_fallback_apply (§15.5), exactly like used_mocks.
    used_secret_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    # Break-glass override values supplied at trigger time, AES-GCM, used only for this run (§15.4).
    secret_overrides_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    variable_provenance: Mapped[dict | None] = mapped_column(JSONB, default=None)

    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = created_at_col()


class RunEvent(Base):
    """Fine-grained state-machine record (SPECS §3.6). Written by transition() in the same txn."""

    __tablename__ = "run_events"

    id: Mapped[uuid.UUID] = pk_uuid()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    from_state: Mapped[RunState | None] = mapped_column(
        Enum(RunState, name="run_state"), default=None
    )
    to_state: Mapped[RunState] = mapped_column(Enum(RunState, name="run_state"))
    actor: Mapped[RunEventActor] = mapped_column(Enum(RunEventActor, name="run_event_actor"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    payload: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = created_at_col()
