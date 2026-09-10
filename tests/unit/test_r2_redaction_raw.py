"""Audit R-H04: provider ``raw`` payloads must normalize to a redacted string.

Several providers hand ``PipelineError`` a structured payload (DataForSEO
response envelopes, Exa bodies) while the field was annotated ``str | None``.
A dataclass does not enforce annotations, so the orchestrator's failure
branch called the string-only ``redact()`` with a dict and raised
``TypeError`` *inside the error handler* — the original business failure was
lost and nothing was persisted.

These tests pin the unified contract:

* ``redact_raw`` accepts any shape and always returns a redacted ``str`` or
  ``None`` (valid JSON for structured input, secrets removed recursively);
* ``PipelineError`` normalizes ``raw`` at construction, so every consumer
  (orchestrator, logs, Web UI) sees a string.
"""

from __future__ import annotations

import json

from app.core.exceptions import ErrorCode, PipelineError
from app.core.redaction import REDACTED, redact_raw


class TestRedactRaw:
    def test_none_stays_none(self):
        assert redact_raw(None) is None

    def test_string_is_redacted(self):
        out = redact_raw("Authorization: Bearer sk-super-secret")
        assert out is not None
        assert "sk-super-secret" not in out
        assert REDACTED in out

    def test_dict_is_serialized_and_redacted(self):
        payload = {
            "status_code": 40101,
            "status_message": "authorization failed",
            "api_key": "sk-live-123",
            "nested": {"password": "hunter2", "keep": "visible"},
            "items": ["Bearer abc123", {"token": "t-1"}],
        }

        out = redact_raw(payload)

        assert isinstance(out, str)
        # still machine-readable JSON for debugging / UI display
        parsed = json.loads(out)
        assert parsed["status_code"] == 40101
        assert parsed["status_message"] == "authorization failed"
        assert parsed["nested"]["keep"] == "visible"
        # every secret shape is gone
        for secret in ("sk-live-123", "hunter2", "abc123", "t-1"):
            assert secret not in out
        assert out.count(REDACTED) >= 4

    def test_list_payload_is_serialized(self):
        out = redact_raw([{"id": 1}, {"url": "/uploads/a.webp"}])
        assert out is not None and json.loads(out) == [{"id": 1}, {"url": "/uploads/a.webp"}]

    def test_non_serializable_falls_back_to_str(self):
        out = redact_raw({"obj": object()})
        assert isinstance(out, str)
        assert "obj" in out

    def test_truncates_large_payload(self):
        out = redact_raw({"blob": "x" * 5000}, max_chars=100)
        assert out is not None
        assert len(out) < 200
        assert "truncated" in out


class TestPipelineErrorRawContract:
    def test_dict_raw_is_coerced_to_string(self):
        error = PipelineError(
            ErrorCode.DATAFORSEO_REQUEST_FAILED,
            "status_code=40101",
            raw={"status_code": 40101, "api_key": "sk-leak"},
        )
        assert isinstance(error.raw, str)
        assert "sk-leak" not in error.raw
        json.loads(error.raw)  # valid JSON

    def test_string_raw_unchanged_shape(self):
        error = PipelineError(ErrorCode.EXTRACTOR_FAILED, "boom", raw="plain text")
        assert error.raw == "plain text"

    def test_raw_defaults_to_none(self):
        assert PipelineError(ErrorCode.EXTRACTOR_FAILED, "boom").raw is None
