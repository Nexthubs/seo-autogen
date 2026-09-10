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
    "failed",
    "cancelled",
]


def test_job_status_has_exactly_20_spec_values():
    values = [s.value for s in JobStatus]
    assert values == SPEC_STATUS_VALUES
    assert len(values) == 20


def test_job_status_is_str_enum():
    assert JobStatus.QUEUED == "queued"
    assert isinstance(JobStatus.QUEUED, str)


def test_terminal_states():
    assert JobStatus.READY.is_terminal
    assert JobStatus.FAILED.is_terminal
    assert JobStatus.CANCELLED.is_terminal
    assert not JobStatus.QUEUED.is_terminal
    assert not JobStatus.STRAPI_SYNCING.is_terminal


def test_image_roles():
    assert ImageRole.HERO.value == "hero"
    assert ImageRole.INLINE.value == "inline"


def test_strapi_sync_status():
    assert StrapiSyncStatus.PENDING.value == "pending"
    assert StrapiSyncStatus.DRAFT_CREATED.value == "draft_created"
