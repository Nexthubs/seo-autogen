"""cost tracking

Revision ID: 0009_cost_tracking
Revises: 0008_strapi_sync
Create Date: 2026-09-15

P9-B1 (SEO-AUTO-DEV-SPEC.md section 54): per-LLM-call usage rows plus the
provider cost columns the paid providers report (DataForSEO, Exa, image
generation). The local LLM is tracked with tokens + duration only.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0009_cost_tracking"
down_revision = "0008_strapi_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.VARCHAR(64), nullable=False),
        sa.Column("model", sa.VARCHAR(128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_job_id", "llm_usage", ["job_id"])
    op.create_index("ix_llm_usage_step", "llm_usage", ["step"])

    op.add_column(
        "images",
        sa.Column(
            "provider_cost", sa.Numeric(12, 6), autoincrement=False, nullable=True
        ),
    )
    op.add_column(
        "source_pages",
        sa.Column(
            "provider_cost", sa.Numeric(12, 6), autoincrement=False, nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("source_pages", "provider_cost")
    op.drop_column("images", "provider_cost")
    op.drop_index("ix_llm_usage_step", table_name="llm_usage")
    op.drop_index("ix_llm_usage_job_id", table_name="llm_usage")
    op.drop_table("llm_usage")
