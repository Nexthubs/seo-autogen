"""Research pipeline ORM models (SEO-AUTO-DEV-SPEC.md sections
46.9-46.12, plus one row per job for the SERP synthesis).

All LLM intermediate results are persisted (spec P4 acceptance:
"Pydantic validated, DB persisted").
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, VARCHAR
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column

#: JSONB on PostgreSQL; plain JSON on other dialects (SQLite unit tests).
_JSONB = JSONB().with_variant(JSON(), "sqlite")


class CompetitorAnalysisRow(Base):
    """LLM analysis of one competitor source (section 46.9)."""

    __tablename__ = "competitor_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_pages.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Pydantic-validated CompetitorAnalysis dump (section 16).
    analysis: Mapped[dict] = mapped_column(_JSONB, nullable=False)

    model: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(VARCHAR(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CompetitorAnalysis job={self.job_id} source={self.source_page_id}>"


class SerpSynthesisRow(Base):
    """SERP synthesis for one job (one row per job, re-generated on
    re-runs of the step).
    """

    __tablename__ = "serp_syntheses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    #: Pydantic-validated SERPSynthesis dump (section 17).
    synthesis: Mapped[dict] = mapped_column(_JSONB, nullable=False)

    model: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(VARCHAR(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SerpSynthesis job={self.job_id}>"


class EvidenceNoteRow(Base):
    """One factual evidence note (section 46.10)."""

    __tablename__ = "evidence_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source_title: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    confidence: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    usage: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EvidenceNote job={self.job_id} claim={self.claim!r}>"


class ContentBriefRow(Base):
    """Content brief for one job (section 46.11)."""

    __tablename__ = "content_briefs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    #: Pydantic-validated ContentBrief dump (section 22).
    brief: Mapped[dict] = mapped_column(_JSONB, nullable=False)

    model: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(VARCHAR(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ContentBrief job={self.job_id}>"


class ArticleOutlineRow(Base):
    """Article outline for one job (section 46.12)."""

    __tablename__ = "article_outlines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    #: Pydantic-validated ArticleOutline dump (section 23).
    outline: Mapped[dict] = mapped_column(_JSONB, nullable=False)

    model: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(VARCHAR(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    #: True once the programmatic validation of section 23 passed.
    valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Number of repair attempts actually used (0-2, section 23).
    repair_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ArticleOutline job={self.job_id} valid={self.valid}>"
