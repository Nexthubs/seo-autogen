"""P3 unit: keyword dataset service (spec section 19).

- lookup: dataset keyword -> metrics; unknown keyword -> None (SERP-only).
- strategy queries: High Volume / Low KD / High CPC / Long Tail.
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Keyword, KeywordCluster
from app.schemas.keyword import KeywordDatasetQuery
from app.services import keyword_service as ks


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed(session)
        yield session
    engine.dispose()


def _seed(session: Session) -> None:
    cluster = KeywordCluster(name="Attachment", sheet_name="Attachment")
    session.add(cluster)
    session.flush()
    rows = [
        # short, high volume
        Keyword(cluster_id=cluster.id, keyword="attachment", volume=5000, kd=Decimal("40"), cpc=Decimal("1.0"), intent="Informational", source="excel"),
        # long tail, low kd
        Keyword(cluster_id=cluster.id, keyword="how to know if my partner is avoidant", volume=90, kd=Decimal("8"), cpc=Decimal("2.5"), intent="Informational", source="excel"),
        # missing metrics entirely (short keyword)
        Keyword(cluster_id=cluster.id, keyword="no metrics", volume=None, kd=None, cpc=None, intent=None, source="excel"),
    ]
    session.add_all(rows)
    session.commit()


def test_lookup_metrics_exact(db: Session):
    m = ks.lookup_metrics(db, "attachment")
    assert m is not None
    assert m.volume == 5000
    assert m.cluster_name == "Attachment"


def test_lookup_metrics_case_insensitive(db: Session):
    m = ks.lookup_metrics(db, "  ATTACHMENT ")
    assert m is not None
    assert m.keyword == "attachment"


def test_lookup_unknown_keyword_returns_none(db: Session):
    assert ks.lookup_metrics(db, "not in dataset") is None
    assert ks.keyword_metrics_available(db, "not in dataset") is False
    assert ks.keyword_metrics_available(db, "attachment") is True


def test_strategy_high_volume(db: Session):
    res = ks.query_dataset(db, KeywordDatasetQuery(min_volume=1000))
    assert [r.keyword for r in res] == ["attachment"]


def test_strategy_low_kd(db: Session):
    res = ks.query_dataset(db, KeywordDatasetQuery(max_kd=10))
    assert [r.keyword for r in res] == ["how to know if my partner is avoidant"]


def test_strategy_high_cpc(db: Session):
    res = ks.query_dataset(db, KeywordDatasetQuery(min_cpc=Decimal("2")))
    assert [r.keyword for r in res] == ["how to know if my partner is avoidant"]


def test_strategy_long_tail(db: Session):
    res = ks.query_dataset(db, KeywordDatasetQuery(long_tail_only=True, min_word_count=4))
    assert [r.keyword for r in res] == ["how to know if my partner is avoidant"]


def test_strategy_cluster_filter(db: Session):
    res = ks.query_dataset(db, KeywordDatasetQuery(cluster_name="attachment"))
    assert len(res) == 3
    res = ks.query_dataset(db, KeywordDatasetQuery(cluster_name="nope"))
    assert res == []


def test_limit_respected(db: Session):
    res = ks.query_dataset(db, KeywordDatasetQuery(limit=1))
    assert len(res) == 1
    # sorted by volume desc: "attachment" (5000) comes first
    assert res[0].keyword == "attachment"
