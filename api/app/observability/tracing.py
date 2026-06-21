from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.logging import get_logger

_log = get_logger("stackd.tracing")


def setup_tracing(app: FastAPI) -> None:
    """Wire OpenTelemetry tracing when STACKD_OTLP_ENDPOINT is set; a no-op otherwise (§H).

    Spans link an HTTP request → run transition → claim → worker phase events via `run_id`. Imports
    are local so the OTel stack is only touched when tracing is actually enabled."""
    endpoint = get_settings().stackd_otlp_endpoint
    if not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "stackd-api"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument()
    _log.info("tracing enabled", extra={"event": "otel.enabled", "endpoint": endpoint})
