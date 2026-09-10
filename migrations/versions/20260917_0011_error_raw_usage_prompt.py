"""error_raw + llm_usage prompt provenance

Revision ID: 0011_error_raw_usage_prompt
Revises: 0010_prompt_hash
Create Date: 2026-09-17

Audit fixes:
- M11 (spec section 49): ``generation_jobs.error_raw`` keeps the raw
  failure detail (model raw output / exception traceback) that is too
  verbose for ``error_message`` but needed for debugging.
- M09 (spec section 48): ``llm_usage`` records the prompt provenance
  (``prompt_name`` / ``prompt_version`` / ``prompt_hash``) of the
  logical LLM call, so usage rows can be attributed to the exact prompt
  content that produced them.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_error_raw_usage_prompt"
down_revision = "0010_prompt_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_jobs",
        sa.Column("error_raw", sa.Text(), nullable=True),
    )
    op.add_column(
        "llm_usage",
        sa.Column("prompt_name", sa.VARCHAR(64), nullable=True),
    )
    op.add_column(
        "llm_usage",
        sa.Column("prompt_version", sa.VARCHAR(32), nullable=True),
    )
    op.add_column(
        "llm_usage",
        sa.Column("prompt_hash", sa.VARCHAR(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "prompt_hash")
    op.drop_column("llm_usage", "prompt_version")
    op.drop_column("llm_usage", "prompt_name")
    op.drop_column("generation_jobs", "error_raw")
