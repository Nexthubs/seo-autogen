"""SERP runs/results and source pages (P2)

Tables per SEO-AUTO-DEV-SPEC.md sections 46.5-46.8:
serp_runs, serp_results, source_pages, job_sources.

Revision ID: 0003_serp_sources
Revises: 0002_generation_jobs
Create Date: 2026-09-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0003_serp_sources"
down_revision: Union[str, None] = "0002_generation_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "serp_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.VARCHAR(length=64), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("location_code", sa.Integer(), nullable=False),
        sa.Column("language_code", sa.VARCHAR(length=16), nullable=False),
        sa.Column("device", sa.VARCHAR(length=16), nullable=False),
        sa.Column("raw_response", JSONB(), nullable=False),
        sa.Column("provider_cost", sa.Numeric(12, 6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_serp_runs_job_id", "serp_runs", ["job_id"])

    op.create_table(
        "serp_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "serp_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("serp_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("result_type", sa.VARCHAR(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("normalized_url", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("raw_item", JSONB(), nullable=False),
    )
    op.create_index("ix_serp_results_run_id", "serp_results", ["serp_run_id"])

    op.create_table(
        "source_pages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.CHAR(length=64), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("content_markdown", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("extractor", sa.VARCHAR(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_pages_url_hash", "source_pages", ["url_hash"])

    op.create_table(
        "job_sources",
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "source_page_id",
            UUID(as_uuid=True),
            sa.ForeignKey("source_pages.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("serp_rank", sa.Integer(), nullable=True),
        sa.Column("source_role", sa.VARCHAR(length=32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("job_sources")
    op.drop_index("ix_source_pages_url_hash", table_name="source_pages")
    op.drop_table("source_pages")
    op.drop_index("ix_serp_results_run_id", table_name="serp_results")
    op.drop_table("serp_results")
    op.drop_index("ix_serp_runs_job_id", table_name="serp_runs")
    op.drop_table("serp_runs")
