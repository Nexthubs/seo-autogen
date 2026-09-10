"""RQ worker entrypoint (P8 — SEO-AUTO-DEV-SPEC.md sections 55, 58).

Boots the RQ worker against the configured queue. The worker processes:

* ``app.workers.article_tasks.process_job`` — the full 15-step pipeline
  (spec section 9), enqueued when a job is created (P8).
* ``app.workers.article_tasks.sync_strapi_draft`` — the on-demand Strapi
  draft push (spec section 43.5), enqueued from the job detail page.

Run: python -m app.workers.article_worker
"""

import logging

from rq import Queue
from rq.worker import Worker
from redis import Redis

from app.core.config import get_settings
from app.core.logging import log_context, setup_logging
from app.workers import article_tasks  # noqa: F401 - task registry


def run_worker() -> None:
    settings = get_settings()
    setup_logging()

    log_context(step="worker", provider="rq")
    logger = logging.getLogger(__name__)

    connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=connection)

    worker = Worker([queue], connection=connection)
    logger.info(
        "worker_started",
        extra={"event": "worker_started", "queue": settings.rq_queue_name},
    )
    worker.work()


if __name__ == "__main__":
    run_worker()
