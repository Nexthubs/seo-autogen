"""Error codes and exceptions (SEO-AUTO-DEV-SPEC.md section 52).

Business failures carry a stable error code, never just an exception
string. Codes are the vocabulary shared by DB (error_code), logging
(error_code field) and the Web UI.
"""

from dataclasses import dataclass
from enum import Enum

from app.core.redaction import redact_raw


class ErrorCode(str, Enum):
    """Stable machine-readable error codes (spec section 52)."""

    # DataForSEO
    DATAFORSEO_AUTH_FAILED = "DATAFORSEO_AUTH_FAILED"
    DATAFORSEO_REQUEST_FAILED = "DATAFORSEO_REQUEST_FAILED"
    DATAFORSEO_EMPTY_SERP = "DATAFORSEO_EMPTY_SERP"

    # Extractor
    EXTRACTOR_AUTH_FAILED = "EXTRACTOR_AUTH_FAILED"
    EXTRACTOR_FAILED = "EXTRACTOR_FAILED"
    SOURCE_EMPTY = "SOURCE_EMPTY"

    # LLM
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_STRUCTURED_OUTPUT_INVALID = "LLM_STRUCTURED_OUTPUT_INVALID"
    LLM_CONTEXT_OVERFLOW = "LLM_CONTEXT_OVERFLOW"

    # Article
    ARTICLE_VALIDATION_FAILED = "ARTICLE_VALIDATION_FAILED"

    # Image
    IMAGE_PROVIDER_FAILED = "IMAGE_PROVIDER_FAILED"
    IMAGE_PLAN_INVALID = "IMAGE_PLAN_INVALID"

    # Strapi
    STRAPI_AUTH_FAILED = "STRAPI_AUTH_FAILED"
    STRAPI_SCHEMA_MISMATCH = "STRAPI_SCHEMA_MISMATCH"
    STRAPI_SLUG_CONFLICT = "STRAPI_SLUG_CONFLICT"
    STRAPI_UPLOAD_FAILED = "STRAPI_UPLOAD_FAILED"
    STRAPI_DRAFT_CREATE_FAILED = "STRAPI_DRAFT_CREATE_FAILED"
    STRAPI_DRAFT_UPDATE_FAILED = "STRAPI_DRAFT_UPDATE_FAILED"


@dataclass
class PipelineError(Exception):
    """A pipeline failure with a stable error code.

    ``raw`` optionally keeps provider/model raw output or payload for
    debugging (spec section 49: keep raw output when structured output
    validation keeps failing).

    The contract for ``raw`` is **always a redacted string or None**
    (audit R-H04). Providers may still *pass* a structured payload (a
    DataForSEO response envelope, an Exa body); ``__post_init__`` runs it
    through :func:`app.core.redaction.redact_raw`, which recursively
    redacts secrets and JSON-serializes it. A dataclass does not enforce
    type annotations at runtime, so before R-H04 a dict ``raw`` reached the
    orchestrator's string-only ``redact()`` call and crashed with
    ``TypeError``, losing the original business failure.
    """

    error_code: ErrorCode
    message: str
    raw: str | None = None

    def __post_init__(self) -> None:
        # R-H04: normalize/redact whatever the provider handed over so the
        # field really is the annotated ``str | None`` for every consumer.
        self.raw = redact_raw(self.raw)

    def __str__(self) -> str:
        return f"{self.error_code.value}: {self.message}"
