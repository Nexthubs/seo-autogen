"""ContentExtractor interface (SEO-AUTO-DEV-SPEC.md sections 2.2, 10.3)."""

import abc

from app.schemas.sources import ExtractedPage


class ContentExtractor(abc.ABC):
    """Fetches the article body for a list of given URLs.

    Input must be URLs that came from the SERP provider (or an
    explicit user action). The extractor never decides search
    ranking itself.
    """

    @abc.abstractmethod
    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        """Extract content for each URL; one ``ExtractedPage`` per URL."""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Return True when the extractor is usable."""
