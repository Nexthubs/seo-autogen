"""SERPProvider interface (SEO-AUTO-DEV-SPEC.md section 10.2)."""

import abc

from app.schemas.serp import SERPRequest, SERPResponse


class SERPProvider(abc.ABC):
    """Google SERP ranking source.

    ``SERPProvider`` decides what Google currently ranks.
    It must never be confused with ``ContentExtractor``, which only
    fetches the body of already-selected URLs (spec section 2.2).
    """

    @abc.abstractmethod
    async def search(self, request: SERPRequest) -> SERPResponse:
        """Fetch the Google SERP for ``request.keyword``."""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Return True when the SERP provider is usable."""
