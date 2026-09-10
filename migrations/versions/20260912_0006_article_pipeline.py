"""Article pipeline (P5)

Tables per SEO-AUTO-DEV-SPEC.md sections 46.13-46.14:
article_versions (UNIQUE job_id+version), article_reviews.

Revision ID: 0006_article_pipeline
Revises: 0005_research_pipeline
Create Date: 2026-09-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0006_article_pipeline"
down_revision: Union[str, None] = "0005_research_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "article_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("stage", sa.VARCHAR(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("seo_title", sa.Text(), nullable=False),
        sa.Column("meta_description", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("model", sa.VARCHAR(128), nullable=True),
        sa.Column("prompt_name", sa.VARCHAR(64), nullable=True),
        sa.Column("prompt_version", sa.VARCHAR(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("job_id", "version"),
    )
    op.create_index(
        "ix_article_versions_job_id", "article_versions", ["job_id"]
    )

    op.create_table(
        "article_reviews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "article_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("article_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("review_type", sa.VARCHAR(32), nullable=False),
        sa.Column("review", JSONB(), nullable=False),
        sa.Column("model", sa.VARCHAR(128), nullable=True),
        sa.Column("prompt_version", sa.VARCHAR(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_article_reviews_job_id", "article_reviews", ["job_id"]
    )
    op.create_index(
        "ix_article_reviews_article_version_id",
        "article_reviews",
        ["article_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_article_reviews_article_version_id", table_name="article_reviews"
    )
    op.drop_index("ix_article_reviews_job_id", table_name="article_reviews")
    op.drop_table("article_reviews")
    op.drop_index("ix_article_versions_job_id", table_name="article_versions")
    op.drop_table("article_versions")
