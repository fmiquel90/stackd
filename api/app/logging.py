from __future__ import annotations

import contextvars
import json
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

# Structured JSON logging (one event per line) with a request/worker context and an in-memory
# ring buffer the UI reads via /api/v1/logs. Goal: the simplest possible debugging.

_log_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "log_context", default=None
)

# LogRecord attributes that are NOT user-supplied "extra" fields.
_STD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


def bind_context(**fields: Any) -> contextvars.Token:
    """Merge fields into the current logging context; returns a token to reset()."""
    merged = {**(_log_context.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    return _log_context.set(merged)


def reset_context(token: contextvars.Token) -> None:
    _log_context.reset(token)


def _record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "msg": record.getMessage(),
    }
    out.update(_log_context.get() or {})
    for key, value in record.__dict__.items():
        if key not in _STD_ATTRS and not key.startswith("_"):
            out[key] = value
    if record.exc_info:
        out["exc"] = logging.Formatter().formatException(record.exc_info)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_to_dict(record), default=str)


class PrettyFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = _record_to_dict(record)
        extras = " ".join(
            f"{k}={v}" for k, v in d.items() if k not in {"ts", "level", "logger", "msg"}
        )
        return f"{d['ts']} {d['level']:<5} {d['logger']}: {d['msg']}" + (
            f"  {extras}" if extras else ""
        )


class RingBufferHandler(logging.Handler):
    """Keeps the last N structured records in memory for the /logs panel (per process)."""

    def __init__(self, capacity: int = 5000) -> None:
        super().__init__()
        self.buffer: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(_record_to_dict(record))
        except Exception:
            pass


_ring = RingBufferHandler()


def ring_buffer() -> RingBufferHandler:
    return _ring


def configure_logging(log_format: str = "json", level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(JsonFormatter() if log_format == "json" else PrettyFormatter())
    root.addHandler(stream)
    root.addHandler(_ring)

    # Uvicorn access logs are replaced by our request middleware to avoid duplicates.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    for noisy in ("uvicorn.error", "sqlalchemy.engine", "watchfiles"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
