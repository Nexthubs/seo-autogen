"""Source ORM models (SEO-AUTO-DEV-SPEC.md sections 46.7, 46.8)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    VARCHAR,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SourcePage(Base):
    """One unique fetched page, shared across jobs (section 46.7)."""

    __tablename__ = "source_pages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(
        CHAR(64), unique=True, nullable=False, index=True
    )

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)

    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    extractor: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)

    #: Cost of the LAST fresh extraction of this page (spec section 54).
    #: ``source_pages`` is a TTL cache shared across jobs: cache hits do
    #: not re-pay and leave the column untouched (it may stay NULL).
    provider_cost: Mapped[float | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SourcePage {self.url!r}>"


class JobSource(Base):
    """Join: which source pages belong to a job, in what role (46.8)."""

    __tablename__ = "job_sources"
    __table_args__ = (
        PrimaryKeyConstraint("job_id", "source_page_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_pages.id", ondelete="CASCADE"),
        nullable=False,
    )

    serp_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_role: Mapped[str] = mapped_column(
        VARCHAR(32), nullable=False, default="competitor"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<JobSource job={self.job_id} source={self.source_page_id} "
            f"rank={self.serp_rank} role={self.source_role}>"
        )
