"""P1 integration: generation_jobs table (spec section 46.1).

Runs against the local PostgreSQL (docker container). Skipped when the
database is not reachable, so the suite stays green on CI.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.models import GenerationJob
from app.db.session import SessionLocal, check_database, engine

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)


def test_generation_jobs_table_matches_spec_46_1():
    expected_columns = {
        "id",
        "keyword",
        "language",
        "market",
        "target_function",
        "strategy",
        "author_document_id",
        "category_document_id",
        "image_count_override",
        "status",
        "current_step",
        "keyword_metrics_available",
        "error_code",
        "error_message",
        "error_raw",
        "created_at",
        "started_at",
        "completed_at",
        "updated_at",
    }
    with engine.connect() as conn:
        info = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'generation_jobs'"
            )
        ).scalars()
        actual = set(info)
    assert actual == expected_columns


def test_create_job_roundtrip():
    job = GenerationJob(keyword="best crm", status="queued")
    with SessionLocal() as session:
        session.add(job)
        session.commit()
        job_id = job.id
        assert isinstance(job_id, uuid.UUID)
        assert job.keyword_metrics_available is False
        assert job.created_at is not None

    with SessionLocal() as session:
        loaded = session.get(GenerationJob, job_id)
        assert loaded.keyword == "best crm"
        assert loaded.status == "queued"
        session.delete(loaded)
        session.commit()


def test_evidence_notes_table_has_r_h06_support_columns():
    """Audit R-H06 / migration 0012: support verdict + excerpt columns."""
    with engine.connect() as conn:
        info = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'evidence_notes'"
                )
            ).scalars()
        )
    assert {"verification_status", "supporting_excerpt"} <= info


def test_provider_cost_ledger_table_exists():
    """Audit R-M02 / migration 0014: the append-only paid-cost ledger."""
    with engine.connect() as conn:
        info = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'provider_cost_events'"
                )
            ).scalars()
        )
    assert {
        "id",
        "job_id",
        "provider",
        "step",
        "kind",
        "amount",
        "detail",
        "created_at",
    } <= info
