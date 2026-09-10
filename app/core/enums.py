"""Shared enums (SEO-AUTO-DEV-SPEC.md sections 8, 30)."""

from enum import Enum


class JobStatus(str, Enum):
    """Unified pipeline state machine (spec section 8)."""

    QUEUED = "queued"
    KEYWORD_PREPARING = "keyword_preparing"
    SERP_SEARCHING = "serp_searching"
    SOURCE_EXTRACTING = "source_extracting"
    SERP_ANALYZING = "serp_analyzing"
    EVIDENCE_RESEARCHING = "evidence_researching"
    BRIEF_GENERATING = "brief_generating"
    OUTLINE_GENERATING = "outline_generating"
    ARTICLE_GENERATING = "article_generating"
    SEO_REVIEWING = "seo_reviewing"
    FACT_REVIEWING = "fact_reviewing"
    STYLE_REVIEWING = "style_reviewing"
    ARTICLE_REVISING = "article_revising"
    IMAGE_PLANNING = "image_planning"
    IMAGE_GENERATING = "image_generating"
    READY = "ready"
    STRAPI_SYNCING = "strapi_syncing"
    STRAPI_DRAFT_CREATED = "strapi_draft_created"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELLED)


class ImageRole(str, Enum):
    """System image roles (spec section 3)."""

    HERO = "hero"
    INLINE = "inline"


class StrapiSyncStatus(str, Enum):
    """Draft sync states (spec section 41)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DRAFT_CREATED = "draft_created"
    FAILED = "failed"
