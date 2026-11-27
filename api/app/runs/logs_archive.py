"""Two-tier run logs (SPECS §5.2): the hot `run_logs` table, then S3 `logs/{run_id}/{phase}.log.gz`.

A terminal run's logs are archived by a periodic task; the hot rows are purged
`log_hot_retention_days` after that. Reads go through `load_chunks`, which serves the hot rows and
falls back to the archive once they are gone — callers never see which tier answered.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import RunState
from app.logging import get_logger
from app.models.run import Run
from app.models.run_log import RunLog
from app.statebackend.store import get_object, list_keys, put_object

_log = get_logger("stackd.logs_archive")
_BATCH = 20  # runs archived per tick: bounded work, the rest waits for the next one
# The agent buffers log lines (1 s / 32 KB, §5.1), so its last chunks can land just after the run's
# terminal event: archive only once the run has been terminal this long, or they would be missed.
_ARCHIVE_GRACE = timedelta(seconds=60)
_PHASE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def valid_phase(phase: str) -> bool:
    """A phase name is interpolated into an S3 key: only the worker's own vocabulary is allowed."""
    return bool(_PHASE_RE.fullmatch(phase))


def archive_key(run_id: uuid.UUID, phase: str) -> str:
    return f"logs/{run_id}/{phase}.log.gz"


def _encode(chunks: list[dict]) -> bytes:
    body = "\n".join(json.dumps(c, separators=(",", ":")) for c in chunks)
    return gzip.compress(body.encode())


def _decode(data: bytes) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(data).decode().splitlines() if line]


async def archive_run_logs(session: AsyncSession, now: datetime) -> int:
    """Archive the logs of terminal runs not archived yet (idempotent: a re-upload overwrites the
    same key with the same bytes). Returns the number of runs archived."""
    runs = (
        (
            await session.execute(
                select(Run)
                .where(
                    Run.finished_at.is_not(None),
                    Run.finished_at <= now - _ARCHIVE_GRACE,
                    Run.logs_archived_at.is_(None),
                )
                .order_by(Run.finished_at)
                .limit(_BATCH)
            )
        )
        .scalars()
        .all()
    )
    done = 0
    for run in runs:
        rows = (
            (
                await session.execute(
                    select(RunLog).where(RunLog.run_id == run.id).order_by(RunLog.phase, RunLog.seq)
                )
            )
            .scalars()
            .all()
        )
        by_phase: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_phase[r.phase].append({"seq": r.seq, "section": r.section, "lines": r.lines})
        try:
            for phase, chunks in by_phase.items():
                await put_object(
                    archive_key(run.id, phase), await asyncio.to_thread(_encode, chunks)
                )
        except Exception as exc:  # S3 down: leave the run unarchived, retry on the next tick
            _log.warning(
                "log archive failed",
                extra={"event": "logs.archive_failed", "run_id": str(run.id), "error": str(exc)},
            )
            continue
        run.logs_archived_at = now
        await session.commit()
        done += 1
    await session.commit()
    return done


async def purge_hot_logs(session: AsyncSession, now: datetime) -> int:
    """Delete the hot rows of runs archived more than `log_hot_retention_days` ago. Returns the
    number of rows deleted."""
    cutoff = now - timedelta(days=get_settings().log_hot_retention_days)
    archived = select(Run.id).where(
        Run.logs_archived_at.is_not(None), Run.logs_archived_at <= cutoff
    )
    result = await session.execute(delete(RunLog).where(RunLog.run_id.in_(archived)))
    await session.commit()
    return result.rowcount or 0  # type: ignore[attr-defined]


async def _hot(session: AsyncSession, run: Run) -> bool:
    """The tier is chosen on the run as a whole, not on a filtered result: a poll past the last
    `seq`, or a phase with no lines, answers empty from the hot tier instead of reading S3."""
    return run.logs_archived_at is None or bool(
        await session.scalar(select(exists().where(RunLog.run_id == run.id)))
    )


def _phase_of(key: str) -> str:
    return key.rsplit("/", 1)[-1].removesuffix(".log.gz")


async def load_chunks(
    session: AsyncSession,
    run: Run,
    *,
    phase: str | None = None,
    after_seq: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Chunks `{phase, section, seq, lines}` ordered by (phase, seq), at most `limit`, from the hot
    table if the run still has rows there, otherwise from the S3 archive — where only the phases
    the answer needs are downloaded and decompressed."""
    if await _hot(session, run):
        stmt = select(RunLog).where(RunLog.run_id == run.id)
        if phase:
            stmt = stmt.where(RunLog.phase == phase)
        if after_seq is not None:
            stmt = stmt.where(RunLog.seq > after_seq)
        stmt = stmt.order_by(RunLog.phase, RunLog.seq)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {"phase": r.phase, "section": r.section, "seq": r.seq, "lines": r.lines} for r in rows
        ]
    # Archived and no hot row left: the hot tier was purged, read the archive.
    if phase and not valid_phase(phase):
        return []
    phases = [phase] if phase else sorted(map(_phase_of, await list_keys(f"logs/{run.id}/")))
    out: list[dict] = []
    for name in phases:
        if limit is not None and len(out) >= limit:
            break
        data = await get_object(archive_key(run.id, name))
        if data is None:
            continue
        for c in await asyncio.to_thread(_decode, data):
            if after_seq is None or c["seq"] > after_seq:
                out.append({"phase": name, **c})
    return out if limit is None else out[:limit]


async def list_phases(session: AsyncSession, run: Run) -> list[str]:
    """The run's log phases, in the order the run went through them, from whichever tier holds
    its logs."""
    if await _hot(session, run):
        names = (
            (await session.execute(select(RunLog.phase).where(RunLog.run_id == run.id).distinct()))
            .scalars()
            .all()
        )
    else:
        names = [_phase_of(k) for k in await list_keys(f"logs/{run.id}/")]
    return sorted(names, key=_run_order)


async def iter_text(session: AsyncSession, run: Run, phases: list[str]) -> AsyncIterator[str]:
    """`render_text` of the run's logs, one phase at a time: the download never holds more than
    one phase in memory."""
    for phase in phases:
        yield render_text(await load_chunks(session, run, phase=phase))


# A run's log phases are its states (§4: planning → checking → applying, or running), so they start
# in the state machine's order. Not by the lines' `t`: a time of day without a date, it would list a
# run's apply before its plan once the run crosses midnight.
_PHASE_ORDER = {state.value: i for i, state in enumerate(RunState)}


def _run_order(phase: str) -> tuple[int, str]:
    return _PHASE_ORDER.get(phase, len(_PHASE_ORDER)), phase


def render_text(chunks: list[dict]) -> str:
    """Plain-text rendering for the download: one line per log line, ANSI kept, phases in the
    order a run goes through them (any other phase after, by name), each phase's chunks by seq."""
    by_phase: dict[str, list[dict]] = defaultdict(list)
    for c in chunks:
        by_phase[c["phase"]].append(c)

    parts: list[str] = []
    for phase in sorted(by_phase, key=_run_order):
        parts.append(f"===== {phase} =====")
        for c in sorted(by_phase[phase], key=lambda c: c["seq"]):
            prefix = f"[{c['section']}] " if c.get("section") else ""
            parts.extend(f"{ln.get('t', '')} {prefix}{ln.get('msg', '')}" for ln in c["lines"])
    return "\n".join(parts) + "\n"
