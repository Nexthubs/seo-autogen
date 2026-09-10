"""Keyword dataset schemas (SEO-AUTO-DEV-SPEC.md sections 19, 20, P3)."""

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class KeywordMetrics(BaseModel):
    """Resolved metrics for one keyword in the dataset (section 19.1)."""

    keyword: str
    cluster_name: str
    volume: int | None = None
    kd: Decimal | None = None
    cpc: Decimal | None = None
    intent: str | None = None


class KeywordImportRow(BaseModel):
    """One parsed Excel data row (section 20)."""

    keyword: str
    volume: int | None = None
    kd: Decimal | None = None
    cpc: Decimal | None = None
    intent: str | None = None


class SheetImportResult(BaseModel):
    """Per-sheet import outcome.

    ``warnings`` carry every row/column the importer could not map or
    parse — unrecognised columns are warned about, never guessed
    (section 20: 不能静默猜测).
    """

    sheet_name: str
    cluster_name: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)


class KeywordImportReport(BaseModel):
    """Full report for one .xlsx import (one sheet per cluster, section 20)."""

    file_name: str
    sheets: list[SheetImportResult] = Field(default_factory=list)
    clusters_created: int = 0
    keywords_created: int = 0
    keywords_updated: int = 0
    keywords_skipped: int = 0

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for sheet in self.sheets:
            out.extend(f"[{sheet.sheet_name}] {w}" for w in sheet.warnings)
        return out

    @field_validator("sheets")
    @classmethod
    def _at_least_one_sheet(cls, sheets: list[SheetImportResult]) -> list:
        if not sheets:
            raise ValueError("no sheets found in workbook")
        return sheets


class KeywordDatasetQuery(BaseModel):
    """Filter for strategy-based dataset queries (section 19.1)."""

    min_volume: int | None = None
    max_kd: int | None = None
    min_cpc: Decimal | None = None
    long_tail_only: bool = False
    #: Keywords with 4+ words are treated as long tail.
    min_word_count: int = 4
    cluster_name: str | None = None
    limit: int = 100
