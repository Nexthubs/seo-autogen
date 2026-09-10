"""GenerationJob ORM (SEO-AUTO-DEV-SPEC.md section 46.1).

The pipeline master table: one row per article-generation run.
Other pipeline tables land with their owning phase (P2+).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, VARCHAR
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, updated_at_column


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    keyword: Mapped[str] = mapped_column(Text, nullable=False)

    language: Mapped[str | None] = mapped_column(VARCHAR(16), nullable=True)
    market: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    target_function: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy: Mapped[str | None] = mapped_column(Text, nullable=True)

    author_document_id: Mapped[str | None] = mapped_column(
        VARCHAR(255), nullable=True
    )
    category_document_id: Mapped[str | None] = mapped_column(
        VARCHAR(255), nullable=True
    )

    image_count_override: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    status: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    current_step: Mapped[str | None] = mapped_column(Text, nullable=True)

    keyword_metrics_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Verbatim raw failure detail kept for debugging (spec section 49).
    #: The user-facing cause/advice is derived from ``error_code`` /
    #: ``error_message``; this field never surfaces in the normal UI panel —
    #: it is stored so a failed run can be diagnosed after the fact.
    error_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = updated_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<GenerationJob id={self.id} keyword={self.keyword!r} "
            f"status={self.status}>"
        )
