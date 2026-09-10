"""Internal link rules ORM model (SEO-AUTO-DEV-SPEC.md section 46.4).

During article generation the LLM may only insert markers of the form
``[[INTERNAL_LINK:MARKER]]`` (section 21) — never URLs. The final renderer
validates the marker against this table and resolves it to a markdown link.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, updated_at_column


class InternalLinkRule(Base):
    """One allowed internal link marker (section 46.4 / 21)."""

    __tablename__ = "internal_link_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    marker: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    anchor_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSONB on PostgreSQL; plain JSON fallback for other dialects (SQLite
    # in the unit suite).
    keywords: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<InternalLinkRule {self.marker!r} active={self.active}>"
