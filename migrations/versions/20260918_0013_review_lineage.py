"""review attempt history + revision review lineage

Revision ID: 0013_review_lineage
Revises: 0012_evidence_support_check
Create Date: 2026-09-17

Audit R-M03: re-running a review step used to DELETE the previous verdict
for the same ``(article_version_id, review_type)`` before inserting the new
one, so the review set an already-persisted revision was produced from
disappeared. ``article_reviews`` is now append-only history:

- ``article_reviews.attempt`` (INTEGER, NOT NULL, default 1): the run number
  for that (version, type). The current valid verdict is the highest attempt.
- ``article_versions.based_on_reviews`` (JSONB, nullable): the exact review
  row ids + attempts a ``revision`` version consumed, so each revision can be
  traced to its own inputs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0013_review_lineage"
down_revision = "0012_evidence_support_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "article_reviews",
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "article_versions",
        sa.Column("based_on_reviews", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("article_versions", "based_on_reviews")
    op.drop_column("article_reviews", "attempt")
