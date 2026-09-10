"""JSON structured logging + secret redaction (spec section 53)."""

import io
import json
import logging
import sys

from app.core.logging import JsonLogHandler, log_context


def _capture_and_emit(msg: str, **extra) -> dict:
    out = io.StringIO()
    handler = JsonLogHandler()
    old = sys.stdout
    sys.stdout = out
    try:
        logger = logging.getLogger("test.log")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers.clear()
        logger.addHandler(handler)
        try:
            logger.info(msg, extra=extra)
        finally:
            logger.removeHandler(handler)
    finally:
        sys.stdout = old
    return json.loads(out.getvalue().strip())


def test_log_standard_fields_present():
    record = _capture_and_emit("serp_requested")
    for key in ("timestamp", "level", "event"):
        assert key in record
    assert record["event"] == "serp_requested"
    assert record["level"] == "INFO"


def test_log_context_fields_appear():
    log_context(job_id="job-1", step="serp_searching", provider="dataforseo")
    try:
        record = _capture_and_emit("search_done", duration_ms=1234)
        assert record["job_id"] == "job-1"
        assert record["step"] == "serp_searching"
        assert record["provider"] == "dataforseo"
        assert record["duration_ms"] == 1234
    finally:
        log_context()


def test_secrets_are_redacted():
    record = _capture_and_emit(
        "request failed: Authorization: Bearer abcdef1234567890"
    )
    line = json.dumps(record)
    assert "abcdef1234567890" not in line
    assert "<redacted>" in line
