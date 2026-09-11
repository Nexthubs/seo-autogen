"""article checkpoint invalidation markers

Revision ID: 0015_checkpoint_invalidation
Revises: 0014_provider_cost_ledger
Create Date: 2026-09-19

Retries keep article versions and reviews as append-only audit history.  The
nullable invalidation timestamps make the current dependency chain explicit,
so a stale review/revision cannot satisfy Resume or the READY gate.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_checkpoint_invalidation"
down_revision = "0014_provider_cost_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "article_versions",
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "article_reviews",
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("article_reviews", "invalidated_at")
    op.drop_column("article_versions", "invalidated_at")
