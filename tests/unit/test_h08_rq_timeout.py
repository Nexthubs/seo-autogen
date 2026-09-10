"""Audit H08 / R-H01: RQ jobs must run with the configured ``job_timeout``.

A full 15-step pipeline run exceeds RQ's default 180s job timeout, so every
``queue.enqueue`` call must pass the real RQ control parameter
``job_timeout=settings.rq_job_timeout_seconds``.

The earlier H08 test used a fake queue that only recorded ``kwargs`` and
asserted ``kwargs["timeout"]`` — which happily confirmed the *wrong* call:
RQ 2.x ``Queue.parse_args`` never consumes ``timeout``, so it became a task
keyword argument and ``process_job`` / ``sync_strapi_draft`` raised
``unexpected keyword argument 'timeout'`` before any business logic ran.

These regression tests therefore:

1. capture the real arguments the project helpers hand to ``queue.enqueue``;
2. feed them through the *installed RQ* ``Queue.parse_args`` (no Redis
   connection required) so real RQ semantics decide what the job timeout is
   and what the task kwargs are;
3. bind the resulting args/kwargs against the real task callables to prove
   the worker can actually enter the function.
"""

from __future__ import annotations

import importlib
import inspect

import pytest
from rq import Queue

from app.core.config import Settings

CONFIGURED_TIMEOUT = 4321


def _settings() -> Settings:
    return Settings(rq_job_timeout_seconds=CONFIGURED_TIMEOUT, _env_file=None)


class FakeQueue:
    """Records the exact ``enqueue`` call without touching Redis."""

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


def _resolve_task(f: str):
    """Import the dotted ``pkg.mod.fn`` task path the way RQ would."""
    module_path, _, attr = f.rpartition(".")
    return getattr(importlib.import_module(module_path), attr)


def _parse_real_rq(call: dict):
    """Run the recorded enqueue call through real RQ argument parsing."""
    args = call["args"]
    assert args, "enqueue() must receive at least the task path"
    return Queue.parse_args(args[0], *args[1:], **call["kwargs"])


def test_enqueue_job_uses_rq_job_timeout(fake_queue):
    from app.workers.article_tasks import enqueue_job

    enqueue_job("00000000-0000-0000-0000-000000000001")

    assert len(fake_queue.calls) == 1
    parsed = _parse_real_rq(fake_queue.calls[0])

    # RQ itself resolves the configured timeout...
    assert parsed.timeout == CONFIGURED_TIMEOUT
    # ...and the task kwargs must be free of the bogus ``timeout`` kwarg.
    assert "timeout" not in parsed.kwargs
    assert parsed.kwargs == {}
    assert parsed.args == ("00000000-0000-0000-0000-000000000001", {})

    # The worker must be able to bind the parsed arguments to the real task.
    task = _resolve_task(parsed.func)
    inspect.signature(task).bind(*parsed.args, **parsed.kwargs)


def test_enqueue_job_with_options_binds_and_keeps_timeout(fake_queue):
    from app.workers.article_tasks import enqueue_job

    enqueue_job("00000000-0000-0000-0000-000000000001", options={"retry_step": 9})

    parsed = _parse_real_rq(fake_queue.calls[0])
    assert parsed.timeout == CONFIGURED_TIMEOUT
    assert "timeout" not in parsed.kwargs
    assert parsed.args == ("00000000-0000-0000-0000-000000000001", {"retry_step": 9})

    task = _resolve_task(parsed.func)
    inspect.signature(task).bind(*parsed.args, **parsed.kwargs)


def test_enqueue_strapi_sync_uses_rq_job_timeout(fake_queue):
    from app.workers.article_tasks import enqueue_strapi_sync

    enqueue_strapi_sync("00000000-0000-0000-0000-000000000001")

    parsed = _parse_real_rq(fake_queue.calls[0])
    assert parsed.timeout == CONFIGURED_TIMEOUT
    assert "timeout" not in parsed.kwargs
    assert parsed.args == ("00000000-0000-0000-0000-000000000001",)

    task = _resolve_task(parsed.func)
    inspect.signature(task).bind(*parsed.args, **parsed.kwargs)


def test_rq_parse_args_rejects_plain_timeout_kwarg():
    """Guard the regression itself: ``timeout`` is not an RQ control param.

    If RQ ever starts consuming ``timeout`` this assertion will fail and the
    R-H01 documentation/regression rationale should be revisited.
    """
    parsed = Queue.parse_args(
        "app.workers.article_tasks.process_job", "job-id", {}, timeout=3600
    )
    assert parsed.timeout is None
    assert parsed.kwargs == {"timeout": 3600}
    with pytest.raises(TypeError):
        inspect.signature(_resolve_task(parsed.func)).bind(*parsed.args, **parsed.kwargs)


def test_setting_defaults_to_one_hour():
    assert Settings(_env_file=None).rq_job_timeout_seconds == 3600
