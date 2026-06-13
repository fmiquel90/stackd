from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import UTC, datetime
from typing import Any

_STD = {
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
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "source": "worker",
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class _RingBufferHandler(logging.Handler):
    """Keeps the last N agent log lines in memory so diagnostics can return a tail."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.buffer: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:  # noqa: BLE001
            pass


_ring = _RingBufferHandler()


def recent_logs() -> list[str]:
    return list(_ring.buffer)


def setup() -> None:
    handler = logging.StreamHandler()
    if os.environ.get("STACKD_LOG_FORMAT", "json") == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler, _ring]
    root.setLevel("INFO")


def get_logger(name: str = "stackd.agent") -> logging.Logger:
    return logging.getLogger(name)
