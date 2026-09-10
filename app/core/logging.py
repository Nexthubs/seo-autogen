"""JSON structured logging (SEO-AUTO-DEV-SPEC.md section 53).

Every record carries at least: timestamp, level, job_id, step, provider,
event, duration_ms, error_code.

Secrets (API keys, Authorization headers, DataForSEO password, Strapi
tokens) must never be logged. This module additionally enforces a redaction
pass over any stringified exception/record field that matches a known
secret pattern.
"""

import json
import logging
import re
import sys
import time
from contextvars import ContextVar
from typing import Any

# Context variables carrying the standard log fields. Pipeline steps set
# these via log_context(); the handler reads them at emit time so any
# logger inside the pipeline logs structured fields automatically.
_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})

RESERVED_KEYS = {
    "timestamp",
    "level",
    "job_id",
    "step",
    "provider",
    "event",
    "duration_ms",
    "error_code",
}

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(token\s*[:=]\s*)[^\s,;]+"), r"\1<redacted>"),
]


def _redact(value: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        value = pattern.sub(repl, value)
    return value


def log_context(
    *,
    job_id: str | None = None,
    step: str | None = None,
    provider: str | None = None,
) -> None:
    """Set standard structured-log fields for the current execution context."""
    _log_context.set(
        {
            "job_id": job_id,
            "step": step,
            "provider": provider,
        }
    )


class JsonLogHandler(logging.Handler):
    """Emit each record as a single-line JSON object to stdout."""

    def emit(self, record: logging.LogRecord) -> None:
        ctx = dict(_log_context.get())
        payload: dict[str, Any] = {
            "timestamp": self.format_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        # Standard fields (context first, record attributes override if set)
        for key in ("job_id", "step", "provider", "duration_ms", "error_code"):
            value = getattr(record, key, None)
            if value is None:
                value = ctx.get(key)
            if value is not None:
                payload[key] = value
        # Extra structured fields attached via extra=...
        for key, value in record.__dict__.items():
            if key in RESERVED_KEYS or key in logging.LogRecord(
                "", 0, "", 0, "", (), None
            ).__dict__:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = _redact(self.format(record))
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)
            line = _redact(line)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:  # pragma: no cover - logging must never crash the app
            pass

    @staticmethod
    def format_timestamp(created: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + (
            f".{int((created % 1) * 1000):03d}"
        )


def setup_logging(level: int | str = "INFO") -> None:
    """Configure root logger with the JSON handler (idempotent)."""
    root = logging.getLogger()
    if not any(isinstance(h, JsonLogHandler) for h in root.handlers):
        handler = JsonLogHandler()
        root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else level.upper())
