"""ImageProvider interface (SEO-AUTO-DEV-SPEC.md section 10.4)."""

import abc

from app.schemas.images import GeneratedImage, ImageGenerationRequest


class ImageProvider(abc.ABC):
    """Image generation.

    V1: OpenAI-compatible Images API (``OpenAIImageProvider``, P6).
    """

    @abc.abstractmethod
    async def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        """Generate one image and store it locally; return its metadata."""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Return True when the image provider is usable."""
