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
import sys
import time
from contextvars import ContextVar
from typing import Any

from app.core.redaction import redact, redact_value

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

#: Every structured ``extra=`` field is passed through this redaction pass
#: (audit M10) before it is serialized, so a provider payload that carries
#: ``"api_key": "sk-..."`` (JSON shape), a Bearer echo or a URL with a
#: sensitive query parameter can never reach the log file verbatim.
def _redact(value: str) -> str:
    return redact(value)


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
            "event": _redact(record.getMessage()),
        }
        # Standard fields (context first, record attributes override if set)
        for key in ("job_id", "step", "provider", "duration_ms", "error_code"):
            value = getattr(record, key, None)
            if value is None:
                value = ctx.get(key)
            if value is not None:
                payload[key] = value
        # Extra structured fields attached via extra=...
        # M10: walk each field recursively (dicts/lists/strings) so a
        # provider payload carrying a JSON ``"api_key"`` or a URL with a
        # sensitive query parameter is redacted BEFORE serialization.
        _builtin_keys = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
        for key, value in record.__dict__.items():
            if key in RESERVED_KEYS or key in _builtin_keys:
                continue
            payload[key] = redact_value(value)
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
