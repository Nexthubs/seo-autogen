"""prompt hash recording

Revision ID: 0010_prompt_hash
Revises: 0009_cost_tracking
Create Date: 2026-09-16

P9-B2 (SEO-AUTO-DEV-SPEC.md section 48): record ``prompt_hash`` (SHA256 of
the prompt file) next to the already-recorded ``prompt_name`` /
``prompt_version`` on every persisted LLM result, so a prompt-version
dashboard can show which exact content produced each row.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_prompt_hash"
down_revision = "0009_cost_tracking"
branch_labels = None
depends_on = None

#: The six result tables that record prompt provenance (spec 46).
_TABLES = (
    "competitor_analyses",
    "serp_syntheses",
    "content_briefs",
    "article_outlines",
    "article_versions",
    "article_reviews",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("prompt_hash", sa.VARCHAR(64), nullable=True),
        )
    # images: the planner's provenance was never recorded at all (46.15 only
    # pins the generation fields), so all three columns are new here.
    op.add_column(
        "images",
        sa.Column("prompt_name", sa.VARCHAR(64), nullable=True),
    )
    op.add_column(
        "images",
        sa.Column("prompt_version", sa.VARCHAR(32), nullable=True),
    )
    op.add_column(
        "images",
        sa.Column("prompt_hash", sa.VARCHAR(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("images", "prompt_hash")
    op.drop_column("images", "prompt_version")
    op.drop_column("images", "prompt_name")
    for table in reversed(_TABLES):
        op.drop_column(table, "prompt_hash")
