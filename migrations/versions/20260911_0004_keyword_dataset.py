"""Keyword dataset and internal link rules (P3)

Tables per SEO-AUTO-DEV-SPEC.md sections 46.2-46.4:
keyword_clusters, keywords (UNIQUE(cluster_id, keyword)),
internal_link_rules (UNIQUE marker).

Revision ID: 0004_keyword_dataset
Revises: 0003_serp_sources
Create Date: 2026-09-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0004_keyword_dataset"
down_revision: Union[str, None] = "0003_serp_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "keyword_clusters",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sheet_name", sa.Text(), nullable=False),
        sa.Column("strapi_category_document_id", sa.VARCHAR(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_keyword_clusters_sheet_name",
        "keyword_clusters",
        ["sheet_name"],
        unique=True,
    )

    op.create_table(
        "keywords",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("keyword_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.Column("kd", sa.Numeric(10, 2), nullable=True),
        sa.Column("cpc", sa.Numeric(12, 4), nullable=True),
        sa.Column("intent", sa.VARCHAR(length=64), nullable=True),
        sa.Column("source", sa.VARCHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "cluster_id", "keyword", name="uq_keywords_cluster_keyword"
        ),
    )
    op.create_index("ix_keywords_cluster_id", "keywords", ["cluster_id"])
    op.create_index("ix_keywords_keyword", "keywords", ["keyword"])

    op.create_table(
        "internal_link_rules",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("marker", sa.Text(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=True),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("keywords", JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
    op.create_index(
        "uq_internal_link_rules_marker",
        "internal_link_rules",
        ["marker"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_internal_link_rules_marker", table_name="internal_link_rules")
    op.drop_table("internal_link_rules")
    op.drop_index("ix_keywords_keyword", table_name="keywords")
    op.drop_index("ix_keywords_cluster_id", table_name="keywords")
    op.drop_table("keywords")
    op.drop_index("uq_keyword_clusters_sheet_name", table_name="keyword_clusters")
    op.drop_table("keyword_clusters")
