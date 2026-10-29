"""Structured JSON logging.

One line of JSON per record, which is what every log aggregator wants and what
``grep`` can still read. Configured once at startup; never at import time, so
tests and library consumers keep control of their own handlers.

A note on Windows: the console encoding is often cp1252, which cannot represent
a rupee sign. Log records are therefore written with ``ensure_ascii=True`` and
currency symbols are kept out of log messages entirely - they belong in the API
response and the PDF, not the logs.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

#: Attributes already present on every LogRecord, excluded from the ``extra`` dump.
_STANDARD = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger, replacing any handlers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn installs its own colourised handlers; route them through ours so
    # every line in the stream is parseable JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # The SDK's HTTP layer is chatty at DEBUG and can echo request bodies.
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    """Module-level logger accessor, so callers never import ``logging`` directly."""
    return logging.getLogger(name)
