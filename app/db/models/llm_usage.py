"""LLM usage metering rows (SEO-AUTO-DEV-SPEC.md section 54, P9-B1).

The V1 LLM is a local model with no per-call bill: each logical LLM
call of a pipeline run records one row with the reported token counts
and the measured wall duration. ``generate_structured`` calls that need
JSON repair count as ONE row (tokens accumulated over the repair
attempts) — the metering unit is the logical call, not the HTTP
request.

``step`` is the 15-step checkpoint name the call belongs to; retrying
a step deletes its rows (see ``checkpoints.reset_from_step``) so usage
always reflects the currently committed checkpoint outputs.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, VARCHAR
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column


class LLMUsageRow(Base):
    """One logical LLM call of one pipeline run (spec section 54)."""

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: One of ``checkpoints.STEP_NAMES`` (the step that made the call).
    step: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, index=True)
    #: Model name reported by the provider (best effort, may be None).
    model: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Wall duration of the logical call in milliseconds (including any
    #: structured-output repair attempts).
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LLMUsage job={self.job_id} step={self.step} "
            f"in={self.input_tokens} out={self.output_tokens} "
            f"{self.duration_ms}ms>"
        )
