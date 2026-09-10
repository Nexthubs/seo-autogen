"""Paid-provider cost ledger (SEO-AUTO-DEV-SPEC.md section 54, audit R-M02).

Section 54 requires provider costs to be retained "for later cost/performance
analysis". The per-artifact cost columns (``serp_runs.provider_cost``,
``source_pages.provider_cost``, ``images.provider_cost``) cannot do that on
their own: a checkpoint reset deletes the row (and its cost), and a
regeneration overwrites the old cost in place.

``provider_cost_events`` is therefore an IMMUTABLE, APPEND-ONLY ledger,
independent of every checkpoint:

* a re-run APPENDS a new event; nothing is ever deleted or overwritten, so a
  retry, a bad-image regeneration, an evidence-verification fetch and a cache
  hit are all visible in the history;
* ``amount`` is NULL when the provider did not report a cost (a genuine
  unknown — never coalesced into 0);
* no financial panel is required by V1 (audit: "此项不要求增加财务面板").

``step`` uses the checkpoint step names (``serp_search``, ``source_extract``,
``evidence_research``, ``image_generate``); ``kind`` distinguishes a paid
call (``charged``), a zero-cost cache hit (``cache_hit``) and a reused local
artifact (``reused``).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, VARCHAR, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column


class ProviderCostEventRow(Base):
    """One paid (or explicitly zero-cost) provider event of one job."""

    __tablename__ = "provider_cost_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: dataforseo | exa | tavily | openai_image ...
    provider: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, index=True)
    #: checkpoint step name that incurred the event.
    step: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, index=True)
    #: charged | cache_hit | reused
    kind: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="charged")
    #: USD amount; NULL = provider did not report a cost (unknown, not 0).
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    #: Free-form context (url, filename, keyword …).
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ProviderCostEvent job={self.job_id} {self.provider}/"
            f"{self.step} {self.kind} amount={self.amount}>"
        )
