"""provider cost ledger

Revision ID: 0014_provider_cost_ledger
Revises: 0013_review_lineage
Create Date: 2026-09-17

Audit R-M02: only LLM usage was retained historically; paid provider costs
(DataForSEO SERP, Exa/Tavily extraction, image generation) were lost whenever
a checkpoint reset deleted their row or a regeneration overwrote the cost in
place. The per-artifact cost columns stay (they describe the CURRENT row), and
this append-only ledger records every paid call independently of checkpoints.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "0014_provider_cost_ledger"
down_revision = "0013_review_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_cost_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.VARCHAR(32), nullable=False),
        sa.Column("step", sa.VARCHAR(64), nullable=False),
        sa.Column(
            "kind",
            sa.VARCHAR(16),
            nullable=False,
            server_default="charged",
        ),
        sa.Column("amount", sa.Numeric(12, 6), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_provider_cost_events_job_id", "provider_cost_events", ["job_id"]
    )
    op.create_index(
        "ix_provider_cost_events_provider", "provider_cost_events", ["provider"]
    )
    op.create_index(
        "ix_provider_cost_events_step", "provider_cost_events", ["step"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_cost_events_step", table_name="provider_cost_events")
    op.drop_index(
        "ix_provider_cost_events_provider", table_name="provider_cost_events"
    )
    op.drop_index("ix_provider_cost_events_job_id", table_name="provider_cost_events")
    op.drop_table("provider_cost_events")
