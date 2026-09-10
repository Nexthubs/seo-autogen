"""Research pipeline (P4)

Tables per SEO-AUTO-DEV-SPEC.md sections 46.9-46.12, plus one row per
job for the SERP synthesis (section 17 result persistence):
competitor_analyses, serp_syntheses (UNIQUE job_id), evidence_notes,
content_briefs (UNIQUE job_id), article_outlines (UNIQUE job_id).

Revision ID: 0005_research_pipeline
Revises: 0004_keyword_dataset
Create Date: 2026-09-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0005_research_pipeline"
down_revision: Union[str, None] = "0004_keyword_dataset"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "competitor_analyses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_page_id",
            UUID(as_uuid=True),
            sa.ForeignKey("source_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("analysis", JSONB(), nullable=False),
        sa.Column("model", sa.VARCHAR(length=128), nullable=True),
        sa.Column("prompt_version", sa.VARCHAR(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_competitor_analyses_job_id", "competitor_analyses", ["job_id"]
    )

    op.create_table(
        "serp_syntheses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("synthesis", JSONB(), nullable=False),
        sa.Column("model", sa.VARCHAR(length=128), nullable=True),
        sa.Column("prompt_version", sa.VARCHAR(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_serp_syntheses_job_id",
        "serp_syntheses",
        ["job_id"],
        unique=True,
    )

    op.create_table(
        "evidence_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("source_title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_type", sa.VARCHAR(length=64), nullable=False),
        sa.Column("confidence", sa.VARCHAR(length=16), nullable=False),
        sa.Column("usage", sa.VARCHAR(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_evidence_notes_job_id", "evidence_notes", ["job_id"])

    op.create_table(
        "content_briefs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("brief", JSONB(), nullable=False),
        sa.Column("model", sa.VARCHAR(length=128), nullable=True),
        sa.Column("prompt_version", sa.VARCHAR(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_content_briefs_job_id", "content_briefs", ["job_id"], unique=True
    )

    op.create_table(
        "article_outlines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("outline", JSONB(), nullable=False),
        sa.Column("model", sa.VARCHAR(length=128), nullable=True),
        sa.Column("prompt_version", sa.VARCHAR(length=32), nullable=True),
        sa.Column(
            "valid", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "repair_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_article_outlines_job_id",
        "article_outlines",
        ["job_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_article_outlines_job_id", table_name="article_outlines")
    op.drop_table("article_outlines")
    op.drop_index("uq_content_briefs_job_id", table_name="content_briefs")
    op.drop_table("content_briefs")
    op.drop_index("ix_evidence_notes_job_id", table_name="evidence_notes")
    op.drop_table("evidence_notes")
    op.drop_index("uq_serp_syntheses_job_id", table_name="serp_syntheses")
    op.drop_table("serp_syntheses")
    op.drop_index("ix_competitor_analyses_job_id", table_name="competitor_analyses")
    op.drop_table("competitor_analyses")
