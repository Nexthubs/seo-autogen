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

from sqlalchemy import func, select
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
    """Case-insensitive *exact* dataset lookup. None = SERP-only keyword.

    Audit M05: the lookup previously used ``keyword.ilike(keyword.strip())``
    — a plain string with no wildcard escaping. A keyword containing ``%`` or
    ``_`` (e.g. ``"100% seo tips"``) was matched as a *pattern*, so
    ``lookup_metrics`` could return metrics for a different keyword
    (``"100 seo tips"``) — wrong strategy data fed into the pipeline. The
    match is now ``lower(keyword) == stripped_input.lower()``, i.e. a
    literal comparison with case folding and no wildcard semantics.
    """
    stripped = keyword.strip()
    stmt = (
        select(Keyword)
        .join(KeywordCluster, Keyword.cluster_id == KeywordCluster.id)
        .where(func.lower(Keyword.keyword) == stripped.lower())
    )
    if cluster_name is not None:
        stmt = stmt.where(
            func.lower(KeywordCluster.name) == cluster_name.strip().lower()
        )
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
        # Literal case-insensitive match (audit M05 — no LIKE wildcards).
        stmt = stmt.where(
            func.lower(KeywordCluster.name) == query.cluster_name.strip().lower()
        )
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
