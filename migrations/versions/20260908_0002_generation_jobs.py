"""Generation jobs (P1)

Pipeline master table per SEO-AUTO-DEV-SPEC.md section 46.1.

Revision ID: 0002_generation_jobs
Revises: 0001_initial
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0002_generation_jobs"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("language", sa.VARCHAR(length=16), nullable=True),
        sa.Column("market", sa.VARCHAR(length=64), nullable=True),
        sa.Column("target_function", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("author_document_id", sa.VARCHAR(length=255), nullable=True),
        sa.Column("category_document_id", sa.VARCHAR(length=255), nullable=True),
        sa.Column("image_count_override", sa.Integer(), nullable=True),
        sa.Column("status", sa.VARCHAR(length=64), nullable=False),
        sa.Column("current_step", sa.Text(), nullable=True),
        sa.Column(
            "keyword_metrics_available",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("generation_jobs")
