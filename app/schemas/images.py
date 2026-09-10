"""Image models (SEO-AUTO-DEV-SPEC.md sections 2.4, 30, 31, 46.15).

P1 defines:
  - ``ImageGenerationRequest`` / ``GeneratedImage`` referenced by the
    ``ImageProvider`` interface (section 10.4);
  - ``ImagePlanItem`` / ``ImagePlan`` from section 30, used later by the
    Image Planner (P6) and the count service (section 31).
"""

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator


class ImageGenerationRequest(BaseModel):
    prompt: str
    filename: str
    alt_text: str = ""
    aspect_ratio: str = "16:9"
    width: int | None = None
    height: int | None = None
    #: When set, the image is stored under
    #: ``{data_dir}/articles/{job_id}/images/`` (section 34).
    job_id: str | None = None


class GeneratedImage(BaseModel):
    local_path: str
    filename: str
    mime_type: str
    prompt: str
    provider: str
    provider_request_id: str | None = None
    #: Cost reported by the image provider (spec section 54). Most image
    #: APIs report no per-request cost — stays None then.
    provider_cost: float | None = None


class ImagePlanItem(BaseModel):
    """One planned image (spec section 30)."""

    role: Literal["hero", "inline"]
    purpose: str

    section_heading: str | None = None
    insertion_marker: str | None = None

    filename: str
    alt_text: str
    prompt: str

    aspect_ratio: str


class ImagePlan(BaseModel):
    """Image plan for one article (spec section 30).

    Constraints: 1 <= total_count <= 3; images[0].role == "hero";
    every inline image carries an insertion_marker; total_count
    must equal len(images).
    """

    total_count: int
    images: list[ImagePlanItem]

    @field_validator("total_count")
    @classmethod
    def count_bounds(cls, value: int) -> int:
        if not 1 <= value <= 3:
            raise ValueError("total_count must be between 1 and 3")
        return value

    @field_validator("images")
    @classmethod
    def plan_constraints(cls, value: list[ImagePlanItem]) -> list[ImagePlanItem]:
        if not value:
            raise ValueError("plan must contain at least one image")
        if value[0].role != "hero":
            raise ValueError("first image must be the hero image")
        for item in value[1:]:
            if item.role == "inline" and not item.insertion_marker:
                raise ValueError(
                    "inline images require an insertion_marker"
                )
        return value

    @model_validator(mode="after")
    def total_matches_images(self) -> "ImagePlan":
        if len(self.images) != self.total_count:
            raise ValueError("total_count must equal len(images)")
        return self


class ImagePlanOutput(BaseModel):
    """Structured LLM output of the Image Planner (section 30, 49).

    The planner receives the ceiling from the Image Count Service and
    may only reduce it (section 31): an oversized plan is rejected by
    the pipeline step, not by Pydantic (the cap is runtime config).
    """

    total_count: int = 0
    images: list[ImagePlanItem] = []
