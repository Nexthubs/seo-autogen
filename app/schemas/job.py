"""Create Job contract schemas (P8 — SEO-AUTO-DEV-SPEC.md section 45).

The web form (section 43.1) and the REST API (section 44/45) both funnel
through :class:`CreateJobRequest`. ``image_count_override`` is the manual
"Image Mode" — ``null`` = Auto, otherwise an integer in 1..3 (section 31:
even a manual mode may never exceed 3).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: Strategy values for section 43.1 (Auto / High Volume / Low KD /
#: High CPC / Long Tail / Pillar).
STRATEGY_AUTO = "auto"
STRATEGY_HIGH_VOLUME = "high_volume"
STRATEGY_LOW_KD = "low_kd"
STRATEGY_HIGH_CPC = "high_cpc"
STRATEGY_LONG_TAIL = "long_tail"
STRATEGY_PILLAR = "pillar"

ALL_STRATEGIES: tuple[str, ...] = (
    STRATEGY_AUTO,
    STRATEGY_HIGH_VOLUME,
    STRATEGY_LOW_KD,
    STRATEGY_HIGH_CPC,
    STRATEGY_LONG_TAIL,
    STRATEGY_PILLAR,
)

IMAGE_MODE_AUTO = "auto"

DEFAULT_LANGUAGE = "en"
DEFAULT_MARKET = "US"


class CreateJobRequest(BaseModel):
    """POST /api/jobs body (section 45)."""

    keyword: str = Field(min_length=1, max_length=500)
    language: str | None = DEFAULT_LANGUAGE
    market: str | None = DEFAULT_MARKET
    target_function: str | None = None
    strategy: str = STRATEGY_AUTO
    author_document_id: str | None = None
    category_document_id: str | None = None
    image_count_override: int | None = Field(default=None, ge=1, le=3)

    @field_validator("keyword")
    @classmethod
    def _keyword_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("keyword must not be blank")
        return value

    @field_validator("strategy")
    @classmethod
    def _strategy_known(cls, value: str) -> str:
        value = value.strip() or STRATEGY_AUTO
        if value not in ALL_STRATEGIES:
            raise ValueError(f"unknown strategy: {value}")
        return value

    def effective_image_override(self) -> int | None:
        """The manual Image Mode as an int, or ``None`` for Auto."""
        if self.image_count_override is None:
            return None
        return max(1, min(self.image_count_override, 3))


class CreateJobResponse(BaseModel):
    """POST /api/jobs response (section 45)."""

    job_id: str
    status: str = "queued"
