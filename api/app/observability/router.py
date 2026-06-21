from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser, require_role
from app.config import get_settings
from app.db import get_session
from app.enums import ACTIVE_STATES, Role, RunState, WorkerStatus
from app.logging import ring_buffer
from app.models.run import Run
from app.models.worker import Worker, WorkerPool
from app.observability import metrics

router = APIRouter(prefix="/api/v1", tags=["observability"])
# Prometheus scrapes `GET /metrics` (no prefix, unauthenticated — restrict at the network layer; it
# exposes only bounded counts, never secrets or tfvars). Kept separate from the /api/v1 router.
metrics_router = APIRouter(tags=["observability"])
DbSession = Annotated[AsyncSession, Depends(get_session)]


@metrics_router.get("/metrics")
async def prometheus_metrics(session: DbSession) -> Response:
    # Refresh the gauges from the DB at scrape time (cheap aggregate queries); counters/histograms
    # are kept up to date at their code points.
    by_state = (await session.execute(select(Run.state, func.count()).group_by(Run.state))).all()
    counts = {state.value: int(n) for state, n in by_state}
    for st in RunState:
        metrics.runs_total.labels(state=st.value).set(counts.get(st.value, 0))

    waiting = (
        await session.execute(
            select(func.count())
            .select_from(Run)
            .where(Run.state.in_([RunState.queued, RunState.confirmed]))
        )
    ).scalar_one()
    metrics.queue_depth.set(int(waiting))

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.stackd_worker_offline_seconds)
    online = (
        await session.execute(
            select(func.count())
            .select_from(Worker)
            .where(Worker.last_heartbeat_at.is_not(None), Worker.last_heartbeat_at >= cutoff)
        )
    ).scalar_one()
    metrics.workers_online.set(int(online))

    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@router.get("/health")
async def health(_: CurrentUser, session: DbSession) -> dict:
    """One-glance system health (DB, workers, queue, recent errors)."""
    settings = get_settings()
    now = datetime.now(UTC)

    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    workers = (await session.execute(select(Worker))).scalars().all()
    # Workers are heterogeneous and routed by pool + labels (SPECS §7), so the UI groups by pool and
    # shows each worker's labels (its declared, routable capability — e.g. tool/version/arch).
    pools = {p.id: p for p in (await session.execute(select(WorkerPool))).scalars().all()}
    offline_after = settings.stackd_worker_offline_seconds
    worker_rows = []
    online = 0
    for w in workers:
        secs = (now - w.last_heartbeat_at).total_seconds() if w.last_heartbeat_at else None
        is_online = secs is not None and secs <= offline_after and w.status != WorkerStatus.offline
        online += 1 if is_online else 0
        pool = pools.get(w.pool_id)
        worker_rows.append(
            {
                "id": str(w.id),
                "name": w.name,
                "pool": pool.name if pool else None,
                "pool_labels": pool.labels if pool else None,
                "labels": w.labels,
                "status": w.status.value,
                "online": is_online,
                "last_heartbeat_at": w.last_heartbeat_at.isoformat()
                if w.last_heartbeat_at
                else None,
                "seconds_since_heartbeat": round(secs, 1) if secs is not None else None,
                "version": w.version,
            }
        )

    active = (
        await session.execute(
            select(func.count()).select_from(Run).where(Run.state.in_(list(ACTIVE_STATES)))
        )
    ).scalar_one()
    queued = (
        await session.execute(
            select(func.count()).select_from(Run).where(Run.state == RunState.queued)
        )
    ).scalar_one()

    recent_errors = sum(1 for r in ring_buffer().buffer if r.get("level") in ("ERROR", "WARNING"))

    return {
        "status": "ok" if db_ok else "degraded",
        "env": settings.stackd_env,
        "version": "0.1.0",
        "checks": {"database": "ok" if db_ok else "error"},
        "workers": {"total": len(workers), "online": online, "items": worker_rows},
        "runs": {"active": int(active), "queued": int(queued)},
        "log_buffer": {"size": len(ring_buffer().buffer), "recent_warn_error": recent_errors},
    }


@router.get("/logs", dependencies=[Depends(require_role(Role.admin))])
async def logs(
    level: str | None = None,
    logger: str | None = None,
    event: str | None = None,
    worker_id: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
    q: str | None = None,
    limit: int = 200,
) -> dict:
    """Recent structured log records from the in-process ring buffer (admin, §debug)."""
    records = list(ring_buffer().buffer)
    levels = None
    if level:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        levels = (
            set(order[order.index(level.upper()) :]) if level.upper() in order else {level.upper()}
        )

    def keep(r: dict) -> bool:
        if levels and r.get("level") not in levels:
            return False
        if logger and logger not in str(r.get("logger", "")):
            return False
        if event and r.get("event") != event:
            return False
        if worker_id and r.get("worker_id") != worker_id:
            return False
        if run_id and r.get("run_id") != run_id:
            return False
        if request_id and r.get("request_id") != request_id:
            return False
        if q and q.lower() not in str(r).lower():
            return False
        return True

    filtered = [r for r in records if keep(r)]
    return {"total": len(filtered), "items": filtered[-min(limit, 1000) :][::-1]}
