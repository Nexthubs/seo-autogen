"""Source cache (SEO-AUTO-DEV-SPEC.md section 14.2).

Logic:

    URL exists?
      +-- no  -> extract
      +-- yes
           +-- cache fresh  -> use cache (no extractor call)
           +-- expired      -> re-extract

Default TTL: ``SOURCE_CACHE_TTL_HOURS=168`` (7 days).
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models.source import SourcePage
from app.schemas.sources import ExtractedPage
from app.services.url_normalizer import content_hash, url_hash

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """Treat a naive stored datetime as UTC (SQLite drops ``tzinfo`` on
    round-trip for ``DateTime(timezone=True)``; Postgres returns aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class SourceCache:
    """DB-backed TTL cache over ``source_pages`` (keyed by url_hash)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def ttl(self) -> timedelta:
        return timedelta(hours=self._settings.source_cache_ttl_hours)

    # ------------------------------------------------------------
    # reads
    # ------------------------------------------------------------
    def find_fresh(self, session: Session, url: str) -> SourcePage | None:
        """Return the cached page if it exists and is within TTL, else None."""
        row = self._find(session, url)
        if row is None:
            return None
        if _aware(row.last_fetched_at) + self.ttl() >= _utcnow():
            return row
        return None

    def find_any(self, session: Session, url: str) -> SourcePage | None:
        return self._find(session, url)

    # ------------------------------------------------------------
    # writes
    # ------------------------------------------------------------
    def upsert(self, session: Session, page: ExtractedPage, original_url: str) -> SourcePage:
        """Insert or refresh the ``source_pages`` row for an extracted page.

        ``first_seen_at`` is preserved across refreshes; content fields,
        extractor and ``last_fetched_at`` are updated.
        """
        now = _utcnow()
        row = self._find(session, original_url)
        if row is None:
            row = SourcePage(
                url=original_url,
                normalized_url=page.normalized_url,
                url_hash=url_hash(original_url),
                first_seen_at=now,
            )

        row.title = page.title
        row.domain = urlsplit(page.normalized_url).hostname
        row.content_markdown = page.content_markdown
        row.content_hash = content_hash(page.content_markdown)
        row.extractor = page.extractor
        row.last_fetched_at = now
        session.add(row)
        # Flush so the PK (url) is available to callers building
        # relations (e.g. job_sources) before the outer commit.
        session.flush()
        return row

    # ------------------------------------------------------------
    def _find(self, session: Session, url: str) -> SourcePage | None:
        stmt = select(SourcePage).where(SourcePage.url_hash == url_hash(url))
        return session.scalars(stmt).first()


def page_from_row(row: SourcePage) -> ExtractedPage:
    """Rebuild an ``ExtractedPage`` from a cached DB row."""
    return ExtractedPage(
        url=row.url,
        normalized_url=row.normalized_url,
        title=row.title,
        content_markdown=row.content_markdown,
        extracted_at=row.last_fetched_at,
        extractor=row.extractor,
    )
