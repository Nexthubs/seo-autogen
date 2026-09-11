"""Offline regressions for the audited retry/resume failure combinations."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.exceptions import PipelineError
from app.db.base import Base
from app.db.models.article import ArticleVersionRow
from app.db.models.job import GenerationJob
from app.pipeline import checkpoints
from app.pipeline.orchestrator import run_job_pipeline
from app.pipeline.steps._article_common import latest_article_version
from app.routes.jobs import _reset_artifacts
from tests.integration.test_p9_pipeline import KEYWORD, _payloads, _payloads_from, _providers


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def new_job(db):
    job = GenerationJob(keyword=KEYWORD, status='queued', target_function='coach', strategy='auto')
    db.add(job)
    db.commit()
    return job


async def run(db, job, tmp_path, payloads, **options):
    providers, *rest = _providers(tmp_path, payloads)
    await run_job_pipeline(db, job, providers, settings=rest[-1], **options)


@pytest.mark.asyncio
async def test_full_retry_writer_failure_resumes_with_new_article(db, tmp_path):
    job = new_job(db)
    await run(db, job, tmp_path, _payloads())
    old_revision = latest_article_version(db, job)
    old_id = old_revision.id
    _reset_artifacts(db, job)
    assert not checkpoints.step_done(db, job, 'article_writer')
    assert not checkpoints.step_done(db, job, 'article_reviser')
    assert old_revision.invalidated_at is not None
    with pytest.raises(PipelineError):
        await run(db, job, tmp_path, _payloads()[:-6])
    index = checkpoints.first_incomplete_step(db, job)
    assert index == 9
    await run(db, job, tmp_path, _payloads_from(index), resume_from_step=index)
    assert job.status == 'ready'
    final = latest_article_version(db, job)
    assert final.id != old_id
    assert final.version > old_revision.version
    assert db.get(ArticleVersionRow, old_id) is not None
    assert len(db.scalars(select(ArticleVersionRow)).all()) == 4


@pytest.mark.asyncio
async def test_reviser_failure_resumes_despite_existing_draft_anticopy(db, tmp_path):
    job = new_job(db)
    with pytest.raises(PipelineError):
        await run(db, job, tmp_path, _payloads()[:-2])
    index = checkpoints.first_incomplete_step(db, job)
    assert index == 13
    await run(db, job, tmp_path, _payloads_from(index), resume_from_step=index)
    assert job.status == 'ready'
    assert set(latest_article_version(db, job).based_on_reviews) == {'seo', 'fact', 'style'}
    assert checkpoints.step_done(db, job, 'article_reviser')
