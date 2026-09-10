"""Declarative base and shared column helpers (SEO-AUTO-DEV-SPEC.md section 46)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 2.x declarative base for all models."""


def pg_uuid() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


def created_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
