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


def test_bearer_in_event_redacted():
    record = _capture_and_emit("Authorization: Bearer sk-abcdef0123456789")
    line = json.dumps(record)
    assert "sk-abcdef0123456789" not in line
    assert "<redacted>" in line


def test_url_query_secret_in_event_redacted():
    record = _capture_and_emit(
        "GET https://api.example.com/search?api_key=sk-qqqwww&limit=5 failed"
    )
    line = json.dumps(record)
    assert "sk-qqqwww" not in line
    # the non-sensitive parameter survives
    assert "limit=5" in line


def test_json_quoted_key_pair_redacted():
    record = _capture_and_emit('body: {"api_key": "sk-live-abc123", "n": 1}')
    line = json.dumps(record)
    assert "sk-live-abc123" not in line
    assert "<redacted>" in line


def test_structured_extra_secret_fields_redacted():
    """M10: a dict/list payload passed via extra= is redacted recursively,
    and the JSON-dumped line never carries the raw secret value."""
    record = _capture_and_emit(
        "request failed",
        **{
            "payload": {
                "api_key": "sk-live-xyz789",
                "nested": {"token": "tok-secret-1", "ok": 1},
            }
        },
    )
    line = json.dumps(record)
    assert "sk-live-xyz789" not in line
    assert "tok-secret-1" not in line
    assert record["payload"]["api_key"] == "<redacted>"
    assert record["payload"]["nested"]["token"] == "<redacted>"
    # non-secret siblings are preserved
    assert record["payload"]["nested"]["ok"] == 1


def test_non_secret_fields_not_clobbered():
    """Redaction must not touch ordinary values that merely *mention* a
    secret word (e.g. a human count of api keys)."""
    record = _capture_and_emit("the api keys available count is 5")
    assert "the api keys available count is 5" in record["event"]


def test_kv_password_assignment_redacted():
    record = _capture_and_emit("connect failed: password=hunter22")
    line = json.dumps(record)
    assert "hunter22" not in line
    assert "<redacted>" in line
