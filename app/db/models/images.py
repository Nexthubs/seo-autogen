"""Image pipeline ORM model (SEO-AUTO-DEV-SPEC.md section 46.15).

Every planned image is one row: the plan fields (role, prompt, alt,
aspect ratio, insertion marker) and the generation results (local
path, provider provenance). The Strapi media columns are filled by
P7 (Strapi sync) — they exist in the table from day one so P7 does
not need its own migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Numeric, Text, VARCHAR
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column


class ImageRow(Base):
    """One image for one job (section 46.15)."""

    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    section_heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    insertion_marker: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str] = mapped_column(Text, nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)

    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    provider: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Planner provenance (P9-B2, spec section 48): the image_planner prompt
    #: that produced this plan.
    prompt_name: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(VARCHAR(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    #: Cost reported by the image provider (spec section 54). Most image
    #: APIs report no per-request cost — the column stays NULL then.
    provider_cost: Mapped[float | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )

    # P7 fills these (Strapi media sync) — present from day one.
    strapi_media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strapi_media_document_id: Mapped[str | None] = mapped_column(
        VARCHAR(191), nullable=True
    )
    strapi_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Image job={self.job_id} role={self.role} #{self.sort_order}>"
