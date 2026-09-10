"""Audit H08: RQ jobs must not run with the default 180s timeout.

A full 15-step pipeline run exceeds RQ's default job timeout, so every
``queue.enqueue`` call must pass ``timeout=settings.rq_job_timeout_seconds``
explicitly (``app/workers/article_tasks.py``). All Redis interaction is
mocked — no live broker needed.
"""

import pytest

from app.core.config import Settings


def _settings() -> Settings:
    return Settings(rq_job_timeout_seconds=4321, _env_file=None)


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, *args, **kwargs) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


@pytest.fixture()
def fake_queue(monkeypatch):
    queue = FakeQueue()
    monkeypatch.setattr("app.workers.article_tasks.get_settings", lambda: _settings())
    monkeypatch.setattr("app.workers.article_tasks.get_queue", lambda settings=None: queue)
    return queue


def test_enqueue_job_passes_configured_timeout(fake_queue):
    from app.workers.article_tasks import enqueue_job

    enqueue_job("00000000-0000-0000-0000-000000000001")

    assert len(fake_queue.calls) == 1
    call = fake_queue.calls[0]
    assert call["kwargs"].get("timeout") == 4321
    # dotted RQ 2.x task path with the options dict riding as 2nd arg.
    assert call["args"][0] == "app.workers.article_tasks.process_job"
    assert call["args"][1] == "00000000-0000-0000-0000-000000000001"
    assert call["args"][2] == {}


def test_enqueue_job_with_options_still_passes_timeout(fake_queue):
    from app.workers.article_tasks import enqueue_job

    enqueue_job("00000000-0000-0000-0000-000000000001", options={"retry_step": 9})

    call = fake_queue.calls[0]
    assert call["kwargs"].get("timeout") == 4321
    assert call["args"][2] == {"retry_step": 9}


def test_enqueue_strapi_sync_passes_configured_timeout(fake_queue):
    from app.workers.article_tasks import enqueue_strapi_sync

    enqueue_strapi_sync("00000000-0000-0000-0000-000000000001")

    call = fake_queue.calls[0]
    assert call["kwargs"].get("timeout") == 4321
    assert call["args"][0] == "app.workers.article_tasks.sync_strapi_draft"


def test_setting_defaults_to_one_hour():
    assert _settings_with_defaults().rq_job_timeout_seconds == 3600


def _settings_with_defaults() -> Settings:
    return Settings(_env_file=None)
