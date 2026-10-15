"""Structured (JSON) logging using only the standard library.

Each log line is one JSON object, e.g.
  {"ts": "2026-10-15T05:30:00.123Z", "level": "INFO", "logger": "payops.api",
   "msg": "health check", "database": "ok"}

Machines can filter these lines (by level, investigation id, agent name...),
which plain text logs make hard. Extra fields are passed with
`logger.info("msg", extra={"fields": {...}})`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            # Only the exception type and message; full tracebacks go to DEBUG.
            exc_type, exc_value = record.exc_info[0], record.exc_info[1]
            entry["error"] = f"{exc_type.__name__ if exc_type else 'Error'}: {exc_value}"
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Send JSON logs to stdout. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # The HTTP client logs every request at INFO; our own health log already
    # records the outcome, so keep the client quiet unless something goes wrong.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
