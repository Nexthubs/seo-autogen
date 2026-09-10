"""evidence_notes support check

Revision ID: 0012_evidence_support_check
Revises: 0011_error_raw_usage_prompt
Create Date: 2026-09-17

Audit R-H06: reachability alone did not prove a cited study/number exists.
The evidence verifier now compares the fetched source body against the
proposed ``source_title`` / ``claim`` (title corroboration, number
corroboration, term overlap, contradiction markers) and stores the verdict
plus the corroborating/contradicting excerpt:

- ``evidence_notes.verification_status`` (VARCHAR(16), nullable)
- ``evidence_notes.supporting_excerpt`` (TEXT, nullable)
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_evidence_support_check"
down_revision = "0011_error_raw_usage_prompt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence_notes",
        sa.Column("verification_status", sa.VARCHAR(16), nullable=True),
    )
    op.add_column(
        "evidence_notes",
        sa.Column("supporting_excerpt", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evidence_notes", "supporting_excerpt")
    op.drop_column("evidence_notes", "verification_status")
