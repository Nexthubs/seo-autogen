"""Strapi sync ORM model (SEO-AUTO-DEV-SPEC.md section 46.16).

One row per job (``job_id`` UNIQUE, section 41: idempotent sync).
The row survives a partial failure — in particular
``strapi_document_id`` — so a retry UPDATES the existing Strapi
draft instead of creating a duplicate (section 41, section 64).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    ForeignKey,
    Integer,
    Text,
    VARCHAR,
    CHAR,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, updated_at_column

#: JSONB on PostgreSQL; plain JSON on other dialects (SQLite unit tests).
_JSONB = JSONB().with_variant(JSON(), "sqlite")


class StrapiSyncRow(Base):
    """Strapi draft sync state for one job (section 46.16)."""

    __tablename__ = "strapi_syncs"

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

    #: Numeric Strapi entry id (used as ``refId`` for hero uploads).
    strapi_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Strapi 5 short documentId — the idempotency anchor (section 41).
    strapi_document_id: Mapped[str | None] = mapped_column(
        VARCHAR(191), nullable=True
    )

    #: e.g. ``strapi_draft_created`` / ``strapi_sync_failed`` (section 64).
    sync_status: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)

    #: Last payload sent to Strapi (audit trail, section 46.16).
    last_payload: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)

    #: sha256 hex digest of the last payload sent (section 46.16).
    last_payload_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StrapiSync job={self.job_id} doc={self.strapi_document_id!r}>"
