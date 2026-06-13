from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.audit.router import router as audit_router
from app.auth.router import router as auth_router
from app.config import get_settings
from app.dependencies.router import router as dependencies_router
from app.environments.router import router as environments_router
from app.errors import register_error_handlers
from app.hooks.router import router as hooks_router
from app.ids import uuid7
from app.logging import bind_context, configure_logging, get_logger, reset_context
from app.notifications.router import router as notifications_router
from app.observability.router import router as observability_router
from app.oidc.router import cloud_router, issuer_router
from app.runs.router import router as runs_router
from app.stacks.router import router as stacks_router
from app.statebackend.router import human_router as state_human_router
from app.statebackend.router import tf_router as state_tf_router
from app.users.router import router as users_router
from app.variable_sets.router import router as variable_sets_router
from app.webhooks.router import router as webhooks_router
from app.workers.pools_router import router as worker_admin_router
from app.workers.worker_router import router as worker_router
from app.ws.router import router as ws_router

# The `default` space (SPECS §3.0) is created by the bootstrap/seed step (app.seed), not at
# app startup — migrations run after the API is already accepting connections in dev (DEV §2).

_http_log = get_logger("stackd.http")

# High-frequency endpoints (polling, heartbeats, log ingestion): their successful calls carry no
# signal and would drown the buffer — they're only logged when they error (4xx/5xx).
_QUIET_PREFIXES = (
    "/healthz",
    "/api/v1/health",
    "/api/v1/logs",
    "/api/v1/ws",
    "/api/v1/auth/refresh",
    "/worker/v1/heartbeat",
    "/worker/v1/jobs/",  # claim/events/logs/artifacts — covered by domain logs instead
)


def _access_level(method: str, status: int, duration_ms: float, path: str) -> int:
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    if duration_ms > 1500:
        return logging.WARNING  # slow successful request still worth surfacing
    if any(path.startswith(p) for p in _QUIET_PREFIXES) or method == "GET":
        return logging.DEBUG  # reads & polls: hidden at INFO, available via STACKD_LOG_LEVEL=DEBUG
    return logging.INFO  # mutations


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    task: asyncio.Task | None = None
    if settings.stackd_run_scheduler:
        from app.scheduler.tasks import scheduler_loop

        task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.stackd_log_format, settings.stackd_log_level)
    app = FastAPI(
        lifespan=lifespan,
        title="Stackd API",
        version="0.1.0",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or uuid7().hex
        token = bind_context(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _http_log.exception(
                "request failed",
                extra={"event": "http.error", "method": request.method, "path": request.url.path},
            )
            reset_context(token)
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        level = _access_level(request.method, response.status_code, duration_ms, request.url.path)
        _http_log.log(
            level,
            "request",
            extra={
                "event": "http.request",
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        reset_context(token)
        return response

    register_error_handlers(app)

    if not settings.is_production:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", settings.stackd_public_url],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(stacks_router)
    app.include_router(environments_router)
    app.include_router(variable_sets_router)
    app.include_router(hooks_router)
    app.include_router(runs_router)
    app.include_router(worker_admin_router)
    app.include_router(worker_router)
    app.include_router(state_tf_router)
    app.include_router(state_human_router)
    app.include_router(audit_router)
    app.include_router(dependencies_router)
    app.include_router(notifications_router)
    app.include_router(webhooks_router)
    app.include_router(issuer_router)
    app.include_router(cloud_router)
    app.include_router(observability_router)
    app.include_router(ws_router)

    # dev_auth is removed from the production image build (DEV §3); never mounted in prod.
    if settings.stackd_dev_auth and not settings.is_production:
        from app.auth.dev import router as dev_router

        app.include_router(dev_router)

    return app


app = create_app()
