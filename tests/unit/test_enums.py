"""P1 enum tests (spec sections 8, 30)."""

from app.core.enums import ImageRole, JobStatus, StrapiSyncStatus

SPEC_STATUS_VALUES = [
    "queued",
    "keyword_preparing",
    "serp_searching",
    "source_extracting",
    "serp_analyzing",
    "evidence_researching",
    "brief_generating",
    "outline_generating",
    "article_generating",
    "seo_reviewing",
    "fact_reviewing",
    "style_reviewing",
    "article_revising",
    "image_planning",
    "image_generating",
    "ready",
    "strapi_syncing",
    "strapi_draft_created",
    # M01 (section 64; the canonical enum in section 8 predates it —
    # documented in docs/AUDIT-FIX-PROGRESS.md B6): persisted Strapi
    # sync failure. The pipeline itself succeeded (ready), so the
    # state is NOT pipeline-terminal: full/step/resume retries stay
    # 409, only the dedicated sync retry path applies.
    "strapi_sync_failed",
    "failed",
    "cancelled",
]


def test_job_status_has_exactly_21_spec_values():
    values = [s.value for s in JobStatus]
    assert values == SPEC_STATUS_VALUES
    assert len(values) == 21


def test_job_status_is_str_enum():
    assert JobStatus.QUEUED == "queued"
    assert isinstance(JobStatus.QUEUED, str)


def test_terminal_states():
    assert JobStatus.READY.is_terminal
    assert JobStatus.FAILED.is_terminal
    assert JobStatus.CANCELLED.is_terminal
    assert not JobStatus.QUEUED.is_terminal
    assert not JobStatus.STRAPI_SYNCING.is_terminal
    # M01: strapi_sync_failed is a PERSISTED sync failure — the
    # pipeline already reached ready, so it is not terminal for the
    # pipeline retry/cancel paths (they stay 409).
    assert not JobStatus.STRAPI_SYNC_FAILED.is_terminal


def test_image_roles():
    assert ImageRole.HERO.value == "hero"
    assert ImageRole.INLINE.value == "inline"


def test_strapi_sync_status():
    assert StrapiSyncStatus.PENDING.value == "pending"
    assert StrapiSyncStatus.DRAFT_CREATED.value == "draft_created"
