"""CMSProvider interface (SEO-AUTO-DEV-SPEC.md sections 10.5, 35).

SAFETY CONSTRAINT: this interface intentionally does NOT define
``publish()``. Publishing is always manual in V1 (spec section 2).
Adding a publish method is a spec violation.
"""

import abc

from app.schemas.article import ArticleDocument
from app.schemas.strapi import MediaUploadResult


class CMSProvider(abc.ABC):
    """CMS draft-level access (V1: Strapi)."""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Return True when the CMS is reachable and authenticated."""

    @abc.abstractmethod
    async def create_draft(self, article: ArticleDocument) -> str:
        """Create a draft and return its Strapi documentId.

        The request must explicitly target ``status=draft``.
        """

    @abc.abstractmethod
    async def update_draft(self, document_id: str, article: ArticleDocument) -> None:
        """Update the existing draft (idempotent sync, spec section 41)."""

    @abc.abstractmethod
    async def upload_media(self, *, path: str, alt_text: str = "") -> MediaUploadResult:
        """Upload a local file to the media library and return its URL/id."""
