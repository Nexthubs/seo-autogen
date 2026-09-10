"""Source extraction models (SEO-AUTO-DEV-SPEC.md section 15)."""

from datetime import datetime

from pydantic import BaseModel


class ExtractedPage(BaseModel):
    url: str
    normalized_url: str
    title: str | None = None
    content_markdown: str
    extracted_at: datetime
    extractor: str
    #: Cost reported by the paid extractor for this page (spec section 54).
    #: For batched extractor calls the reported total is attributed to every
    #: page of the batch; the pipeline extracts one URL per call, so batch
    #: total == per-page cost there. None when the extractor reports none.
    provider_cost: float | None = None

    @property
    def word_count(self) -> int:
        return len(self.content_markdown.split())
