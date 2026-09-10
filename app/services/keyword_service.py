"""Keyword dataset service (SEO-AUTO-DEV-SPEC.md section 19, P3).

- Dataset mode: the keyword exists in the imported dataset → volume/kd/cpc
  are available and strategy queries (High Volume / Low KD / High CPC /
  Long Tail) are allowed (section 19.1).
- SERP-only mode: the keyword is NOT in the dataset →
  ``keyword_metrics_available = False``; the job is still creatable, and
  downstream steps must not let the LLM guess metrics (section 19.2).
"""

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.keyword import Keyword, KeywordCluster
from app.schemas.keyword import (
    KeywordDatasetQuery,
    KeywordMetrics,
)

logger = logging.getLogger(__name__)


def lookup_metrics(
    session: Session,
    keyword: str,
    cluster_name: str | None = None,
) -> KeywordMetrics | None:
    """Exact (case-insensitive) dataset lookup. None = SERP-only keyword."""
    stmt = (
        select(Keyword)
        .join(KeywordCluster, Keyword.cluster_id == KeywordCluster.id)
        .where(Keyword.keyword.ilike(keyword.strip()))
    )
    if cluster_name is not None:
        stmt = stmt.where(KeywordCluster.name.ilike(cluster_name))
    row = session.scalars(stmt.order_by(Keyword.volume.desc().nullslast()).limit(1)).first()
    if row is None:
        return None
    return KeywordMetrics(
        keyword=row.keyword,
        cluster_name=row.cluster.name,
        volume=row.volume,
        kd=row.kd,
        cpc=row.cpc,
        intent=row.intent,
    )


def keyword_metrics_available(session: Session, keyword: str) -> bool:
    """P3 acceptance: dataset keyword → True, otherwise False (section 19.2)."""
    return lookup_metrics(session, keyword) is not None


def _word_count(keyword: str) -> int:
    return len([w for w in (keyword or "").split() if w])


def query_dataset(session: Session, query: KeywordDatasetQuery) -> list[KeywordMetrics]:
    """Strategy queries over the dataset (section 19.1).

    High Volume / Low KD / High CPC are pushed to SQL; Long Tail is applied
    in Python (word count) so the query stays dialect-portable.
    """
    stmt = (
        select(Keyword)
        .join(KeywordCluster, Keyword.cluster_id == KeywordCluster.id)
    )
    if query.min_volume is not None:
        stmt = stmt.where(Keyword.volume.is_not(None), Keyword.volume >= query.min_volume)
    if query.max_kd is not None:
        stmt = stmt.where(Keyword.kd.is_not(None), Keyword.kd <= Decimal(query.max_kd))
    if query.min_cpc is not None:
        stmt = stmt.where(Keyword.cpc.is_not(None), Keyword.cpc >= query.min_cpc)
    if query.cluster_name is not None:
        stmt = stmt.where(KeywordCluster.name.ilike(query.cluster_name))
    stmt = stmt.order_by(
        Keyword.volume.desc().nullslast(), Keyword.kd.asc().nullslast()
    )

    rows = session.scalars(stmt).all()
    out: list[KeywordMetrics] = []
    for r in rows:
        if query.long_tail_only and _word_count(r.keyword) < query.min_word_count:
            continue
        out.append(
            KeywordMetrics(
                keyword=r.keyword,
                cluster_name=r.cluster.name,
                volume=r.volume,
                kd=r.kd,
                cpc=r.cpc,
                intent=r.intent,
            )
        )
        if len(out) >= query.limit:
            break
    return out
