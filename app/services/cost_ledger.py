"""Paid-provider cost ledger service (spec section 54, audit R-M02).

One funnel for every paid provider event, so the history is uniform:

```python
record_provider_cost(session, job_id=..., provider="dataforseo",
                     step="serp_search", amount=0.002, detail=keyword)
record_cache_hit(session, job_id=..., provider="exa",
                 step="source_extract", detail=url)
record_reuse(session, job_id=..., provider="openai_image",
             step="image_generate", detail=filename)
```

Rules:

* ``amount=None`` means "the provider did not report a cost" — it stays NULL
  in the ledger (never coalesced into 0);
* rows are appended and flushed; the owning step commits them with its own
  checkpoint transaction;
* nothing in the pipeline ever deletes ledger rows (independent of the
  checkpoint reset).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models.cost import ProviderCostEventRow

#: kinds
CHARGED = "charged"
CACHE_HIT = "cache_hit"
REUSED = "reused"


def _as_decimal(amount: float | int | Decimal | None) -> Decimal | None:
    if amount is None:
        return None
    if isinstance(amount, Decimal):
        return amount
    return Decimal(str(amount))


def _record(
    session: Session,
    *,
    job_id: uuid.UUID,
    provider: str,
    step: str,
    kind: str,
    amount: float | int | Decimal | None,
    detail: str | None,
) -> ProviderCostEventRow:
    row = ProviderCostEventRow(
        job_id=job_id,
        provider=provider,
        step=step,
        kind=kind,
        amount=_as_decimal(amount),
        detail=(detail[:2000] if detail else None),
    )
    session.add(row)
    session.flush()
    return row


def record_provider_cost(
    session: Session,
    *,
    job_id: uuid.UUID,
    provider: str,
    step: str,
    amount: float | int | Decimal | None,
    detail: str | None = None,
) -> ProviderCostEventRow:
    """Append one paid-call event (``amount=None`` = unknown, stays NULL)."""
    return _record(
        session,
        job_id=job_id,
        provider=provider,
        step=step,
        kind=CHARGED,
        amount=amount,
        detail=detail,
    )


def record_cache_hit(
    session: Session,
    *,
    job_id: uuid.UUID,
    provider: str,
    step: str,
    detail: str | None = None,
) -> ProviderCostEventRow:
    """Append a zero-cost event: the cache served the artifact, no call made."""
    return _record(
        session,
        job_id=job_id,
        provider=provider,
        step=step,
        kind=CACHE_HIT,
        amount=Decimal("0"),
        detail=detail,
    )


def record_reuse(
    session: Session,
    *,
    job_id: uuid.UUID,
    provider: str,
    step: str,
    detail: str | None = None,
) -> ProviderCostEventRow:
    """Append a zero-cost event: an existing local artifact was reused."""
    return _record(
        session,
        job_id=job_id,
        provider=provider,
        step=step,
        kind=REUSED,
        amount=Decimal("0"),
        detail=detail,
    )


def total_cost(session: Session, job_id: uuid.UUID) -> Decimal | None:
    """Sum every charged amount; ``None`` when no known cost exists.

    Unknown (NULL) amounts are ignored rather than treated as zero, and a job
    with no reported cost at all returns ``None`` (not ``0``).
    """
    from sqlalchemy import func, select

    value = session.scalar(
        select(func.sum(ProviderCostEventRow.amount)).where(
            ProviderCostEventRow.job_id == job_id
        )
    )
    return value
