"""FastAPI application entrypoint (P8 — spec sections 43, 44, 55).

Mounts the P8 web UI (Jinja2 + HTMX) and the REST API routers, plus
``/health``. The application does not depend on the CLI: the worker is a
separate RQ entrypoint (``app.workers.article_worker``) that consumes the
same job table.
"""

from __future__ import annotations

from pathlib import Path

import redis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import check_database
from app.routes import datasets, jobs, providers, prompts, strapi, web

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()

    app = FastAPI(
        title="SEO Article Automation System",
        version="0.1.0",
        description=(
            "Deterministic, checkpointed SEO content production pipeline. "
            "Spec: SEO-AUTO-DEV-SPEC.md"
        ),
    )
    app.state.settings = settings

    @app.get("/health")
    def health() -> dict:
        """Liveness + dependency reachability (spec section 58)."""
        return {
            "status": "ok",
            "database": check_database(),
            "redis": check_redis(settings.redis_url),
        }

    # Web routes first so the specific ``/static/job-images/...`` route in
    # app.routes.web wins over the StaticFiles mount below.
    app.include_router(web.router)
    app.include_router(jobs.router)
    app.include_router(datasets.router)
    app.include_router(providers.router)
    app.include_router(prompts.router)
    app.include_router(strapi.router)

    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    return app


def check_redis(redis_url: str) -> bool:
    try:
        client = redis.Redis.from_url(redis_url, socket_connect_timeout=3)
        client.ping()
        return True
    except Exception:
        return False


app = create_app()
