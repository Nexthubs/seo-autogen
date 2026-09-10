"""Keyword dataset ORM models (SEO-AUTO-DEV-SPEC.md sections 46.2, 46.3).

A SEMrush Excel sheet becomes one ``keyword_clusters`` row (topic cluster);
every data row becomes a ``keywords`` row. Re-imports upsert on
``UNIQUE(cluster_id, keyword)`` — no duplicate records (spec section 20 / P3).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    VARCHAR,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column


class KeywordCluster(Base):
    """Topic cluster; one per Excel sheet (section 46.2)."""

    __tablename__ = "keyword_clusters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sheet_name: Mapped[str] = mapped_column(Text, nullable=False)
    strapi_category_document_id: Mapped[str | None] = mapped_column(
        VARCHAR(255), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    keywords: Mapped[list["Keyword"]] = relationship(
        back_populates="cluster",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<KeywordCluster {self.name!r} sheet={self.sheet_name!r}>"


class Keyword(Base):
    """One keyword row with SEMrush metrics (section 46.3)."""

    __tablename__ = "keywords"
    __table_args__ = (
        UniqueConstraint("cluster_id", "keyword", name="uq_keywords_cluster_keyword"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("keyword_clusters.id", ondelete="CASCADE"),
        nullable=False,
    )

    keyword: Mapped[str] = mapped_column(Text, nullable=False)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    cpc: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    intent: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    #: Where this row came from, e.g. "excel" (section 46.3 ``source``).
    source: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    cluster: Mapped[KeywordCluster] = relationship(
        back_populates="keywords", lazy="joined"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Keyword {self.keyword!r} volume={self.volume}>"
