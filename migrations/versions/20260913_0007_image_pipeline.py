"""Image pipeline (P6)

Table per SEO-AUTO-DEV-SPEC.md section 46.15: images.

Revision ID: 0007_image_pipeline
Revises: 0006_article_pipeline
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0007_image_pipeline"
down_revision: Union[str, None] = "0006_article_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.VARCHAR(16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("section_heading", sa.Text(), nullable=True),
        sa.Column("insertion_marker", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=False),
        sa.Column("aspect_ratio", sa.VARCHAR(16), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.VARCHAR(64), nullable=True),
        sa.Column("provider", sa.VARCHAR(64), nullable=False),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("strapi_media_id", sa.Integer(), nullable=True),
        sa.Column("strapi_media_document_id", sa.VARCHAR(191), nullable=True),
        sa.Column("strapi_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_images_job_id", "images", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_images_job_id", table_name="images")
    op.drop_table("images")
