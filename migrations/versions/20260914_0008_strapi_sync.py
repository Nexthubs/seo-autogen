"""Strapi sync (P7)

Table per SEO-AUTO-DEV-SPEC.md section 46.16: strapi_syncs.

Revision ID: 0008_strapi_sync
Revises: 0007_image_pipeline
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0008_strapi_sync"
down_revision: Union[str, None] = "0007_image_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strapi_syncs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("strapi_id", sa.Integer(), nullable=True),
        sa.Column("strapi_document_id", sa.VARCHAR(191), nullable=True),
        sa.Column("sync_status", sa.VARCHAR(64), nullable=False),
        sa.Column("last_payload", sa.JSON(), nullable=True),
        sa.Column("last_payload_hash", sa.CHAR(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_strapi_syncs_job_id", "strapi_syncs", ["job_id"])
    op.create_unique_constraint(
        "uq_strapi_syncs_job_id", "strapi_syncs", ["job_id"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_strapi_syncs_job_id", "strapi_syncs", type_="unique"
    )
    op.drop_index("ix_strapi_syncs_job_id", table_name="strapi_syncs")
    op.drop_table("strapi_syncs")
