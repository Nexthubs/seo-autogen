"""Article pipeline ORM models (SEO-AUTO-DEV-SPEC.md sections
46.13-46.14).

Article Versioning (section 28): every version the pipeline produces
(v1 writer, v2 revision, v3 manual regeneration, ...) is persisted —
nothing is ever overwritten.

Every review (section 26) is persisted as a row in ``article_reviews``
tied to the article version it reviewed.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, VARCHAR
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column

#: JSONB on PostgreSQL; plain JSON on other dialects (SQLite unit tests).
_JSONB = JSONB().with_variant(JSON(), "sqlite")


class ArticleVersionRow(Base):
    """One article version for one job (section 46.13)."""

    __tablename__ = "article_versions"
    __table_args__ = (UniqueConstraint("job_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: e.g. "writer" (v1), "revision" (v2), "manual_regeneration".
    stage: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    seo_title: Mapped[str] = mapped_column(Text, nullable=False)
    meta_description: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)

    model: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(VARCHAR(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    #: R-M03: which review rows (per type) this revision was produced from.
    #: A retry of a review appends a new attempt instead of deleting the old
    #: verdict, so a revision must record the exact attempt set it consumed —
    #: otherwise an old revision cannot be traced to the reviews that shaped
    #: it once newer attempts exist. Shape:
    #: ``{"seo": {"review_id": "<uuid>", "attempt": 1}, ...}``.
    based_on_reviews: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)

    #: A retry never deletes immutable history. Instead, versions made stale
    #: by re-running the writer/reviewer chain are explicitly invalidated and
    #: excluded from the current checkpoint/shipping view.
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    @property
    def article_document(self) -> dict:
        """The version's fields as an ArticleDocument-shaped dict."""
        return {
            "title": self.title,
            "body_markdown": self.body_markdown,
            "seo_title": self.seo_title,
            "meta_description": self.meta_description,
            "slug": self.slug,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ArticleVersion job={self.job_id} v{self.version} {self.stage}>"


class ArticleReviewRow(Base):
    """One reviewer verdict for one article version (section 46.14)."""

    __tablename__ = "article_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    article_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("article_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: "seo" | "fact" | "style" | "anticopy"
    review_type: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)

    #: Pydantic-validated review dump (section 26 / 29).
    review: Mapped[dict] = mapped_column(_JSONB, nullable=False)

    model: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(VARCHAR(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)

    #: R-M03: append-only run history. Re-running a review for the same
    #: (version, type) appends attempt N+1 instead of deleting attempt N, so
    #: an older revision can still be traced to the review set it used. The
    #: "current valid" verdict is the highest attempt.
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    #: Append-only review history is retained across retries.  This marker
    #: distinguishes an auditable historical attempt from the attempt that is
    #: currently valid for checkpoint and revision-lineage purposes.
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ArticleReview job={self.job_id} type={self.review_type}>"
