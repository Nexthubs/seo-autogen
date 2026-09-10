"""SERP ORM models (SEO-AUTO-DEV-SPEC.md sections 46.5, 46.6)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    VARCHAR,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB, UUID

#: JSONB on PostgreSQL; plain JSON on other dialects (SQLite unit tests).
_JSONB = JSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column


class SerpRun(Base):
    """One SERP provider request for a job (section 46.5)."""

    __tablename__ = "serp_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)

    location_code: Mapped[int] = mapped_column(Integer, nullable=False)
    language_code: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    device: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)

    #: Full raw provider payload (spec section 12.2): kept so the parsed
    #: results can be re-derived without another paid SERP call.
    raw_response: Mapped[dict] = mapped_column(_JSONB, nullable=False)
    provider_cost: Mapped[float | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    results: Mapped[list["SerpResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SerpRun id={self.id} query={self.query!r}>"


class SerpResult(Base):
    """One normalized SERP item (organic / paa / related / featured)."""

    __tablename__ = "serp_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    serp_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("serp_runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    result_type: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Raw provider item, kept for re-parsing (spec section 12.2).
    raw_item: Mapped[dict] = mapped_column(_JSONB, nullable=False)

    run: Mapped[SerpRun] = relationship(back_populates="results")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SerpResult {self.result_type} rank={self.rank} "
            f"url={self.url!r}>"
        )
