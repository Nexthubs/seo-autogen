"""SEMrush Excel import (SEO-AUTO-DEV-SPEC.md sections 19, 20, P3).

Rules:
- One workbook sheet = one topic cluster (section 20: Sheet -> cluster).
- Header names are mapped through an alias table (section 20).
- Unrecognised columns produce a *warning*; they are never guessed at.
- Re-importing the same file is an upsert on
  ``UNIQUE(cluster_id, keyword)`` — no duplicate records.

The importer is pure parsing + DB upsert; it never calls an LLM.
"""

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.keyword import Keyword, KeywordCluster
from app.schemas.keyword import (
    KeywordImportReport,
    KeywordImportRow,
    SheetImportResult,
)

logger = logging.getLogger(__name__)

#: Canonical field -> accepted header aliases (section 20).
#: Keys and aliases are compared case-insensitively with whitespace
#: collapsed; ``KD %`` is matched after stripping ``%``.
COLUMN_ALIASES: dict[str, frozenset[str]] = {
    "keyword": frozenset({"keyword", "kw"}),
    "volume": frozenset({"volume", "search volume", "search_volume"}),
    "kd": frozenset({"kd", "keyword difficulty", "kd %", "difficulty"}),
    "cpc": frozenset({"cpc"}),
    "intent": frozenset({"intent", "search intent"}),
}

REQUIRED_COLUMN = "keyword"
IMPORT_SOURCE = "excel"


def _canonical_header(header: object) -> str | None:
    """Normalize a raw header cell to its canonical field name."""
    if header is None:
        return None
    text = str(header).strip().lower().replace("\n", " ")
    text = " ".join(text.split())
    bare = text.replace("%", "").strip()
    for field, aliases in COLUMN_ALIASES.items():
        if text in aliases or bare in aliases:
            return field
    return None


def _parse_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(Decimal(text))
    except InvalidOperation:
        return None


def _parse_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_workbook_with_rows(
    path: Path | str,
) -> tuple[list[SheetImportResult], dict[str, list[KeywordImportRow]]]:
    """Parse a .xlsx workbook into per-sheet report shells + rows (section 20)."""
    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    results: list[SheetImportResult] = []
    rows_by_sheet: dict[str, list[KeywordImportRow]] = {}

    for sheet in workbook.worksheets:
        rows, warnings = _parse_sheet(sheet)
        results.append(
            SheetImportResult(
                sheet_name=sheet.title,
                cluster_name=sheet.title,
                warnings=warnings,
            )
        )
        rows_by_sheet[sheet.title] = rows

    workbook.close()
    return results, rows_by_sheet


def _parse_sheet(sheet) -> tuple[list[KeywordImportRow], list[str]]:
    rows: list[KeywordImportRow] = []
    warnings: list[str] = []
    header_row = None
    mapping: dict[str, int] = {}
    unknown: list[str] = []

    for row_idx, raw in enumerate(sheet.iter_rows(values_only=True), start=1):
        if header_row is None:
            if raw is None or all(v is None or str(v).strip() == "" for v in raw):
                continue  # skip leading blank lines before the header
            header_row = raw
            for col_idx, cell in enumerate(raw):
                field = _canonical_header(cell)
                if field is None:
                    if cell is not None and str(cell).strip() != "":
                        unknown.append(str(cell).strip())
                    continue
                if field not in mapping:
                    mapping[field] = col_idx
            if unknown:
                warnings.append(
                    "unrecognised column(s) ignored: " + ", ".join(f"'{u}'" for u in unknown)
                )
            if REQUIRED_COLUMN not in mapping:
                warnings.append(
                    "missing required column 'Keyword' — sheet skipped"
                )
            continue

        if raw is None or all(v is None or str(v).strip() == "" for v in raw):
            continue  # blank data row

        def cell(field: str) -> object:
            idx = mapping.get(field)
            if idx is None or idx >= len(raw):
                return None
            return raw[idx]

        keyword = cell("keyword")
        keyword_text = str(keyword).strip() if keyword is not None else ""
        if not keyword_text:
            warnings.append(f"row {row_idx}: empty keyword — skipped")
            continue

        volume = _parse_int(cell("volume"))
        kd = _parse_decimal(cell("kd"))
        cpc = _parse_decimal(cell("cpc"))
        intent_raw = cell("intent")
        intent = str(intent_raw).strip() if intent_raw is not None else None

        raw_volume = cell("volume")
        if raw_volume is not None and raw_volume != "" and volume is None:
            warnings.append(f"row {row_idx}: unparseable volume {raw_volume!r}")
        raw_kd = cell("kd")
        if raw_kd is not None and raw_kd != "" and kd is None:
            warnings.append(f"row {row_idx}: unparseable KD {raw_kd!r}")
        raw_cpc = cell("cpc")
        if raw_cpc is not None and raw_cpc != "" and cpc is None:
            warnings.append(f"row {row_idx}: unparseable CPC {raw_cpc!r}")

        rows.append(
            KeywordImportRow(
                keyword=keyword_text,
                volume=volume,
                kd=kd,
                cpc=cpc,
                intent=intent or None,
            )
        )
    return rows, warnings


def import_workbook(session: Session, path: Path | str, file_name: str | None = None) -> KeywordImportReport:
    """Parse + upsert a .xlsx workbook (section 20; P3 acceptance)."""
    results, rows_by_sheet = parse_workbook_with_rows(path)
    if not results:
        raise ValueError("workbook has no sheets")

    report = KeywordImportReport(file_name=file_name or str(path))
    report.sheets = []

    for shell in results:
        cluster = session.scalar(
            select(KeywordCluster).where(KeywordCluster.sheet_name == shell.sheet_name)
        )
        cluster_created = False
        if cluster is None:
            cluster = KeywordCluster(name=shell.cluster_name, sheet_name=shell.sheet_name)
            session.add(cluster)
            session.flush()
            cluster_created = True

        sheet_result = SheetImportResult(
            sheet_name=shell.sheet_name,
            cluster_name=cluster.name,
            warnings=list(shell.warnings),
        )
        # Audit M05 — in-sheet dedup before insert. The production session
        # runs with ``autoflush=False``: two rows in the same sheet whose
        # keywords differ only in case (e.g. "SEO Tips" / "seo tips") are
        # both "new" on the DB lookup, both get added, and the final commit
        # hits ``UNIQUE(cluster_id, keyword)`` → IntegrityError. Deduping
        # case-insensitively in memory (last row wins, a warning is emitted)
        # makes the insert safe regardless of flush policy.
        rows = rows_by_sheet.get(shell.sheet_name, [])
        deduped: dict[str, int] = {}
        for idx, row in enumerate(rows):
            key = row.keyword.casefold()
            if key in deduped:
                first = rows[deduped[key]]
                sheet_result.warnings.append(
                    f"duplicate keyword '{row.keyword}' in sheet "
                    f"'{shell.sheet_name}' — using last value "
                    f"(first seen: '{first.keyword}')"
                )
            deduped[key] = idx
        for idx, row in enumerate(rows):
            if deduped[row.keyword.casefold()] != idx:
                continue  # superseded by a later case-insensitive duplicate
            # Case-insensitive *exact* match so re-imports never duplicate
            # and literal ``%``/``_`` in keywords are not treated as LIKE
            # wildcards (audit M05 — the old ``.ilike(keyword)`` was both).
            existing = session.scalar(
                select(Keyword)
                .where(
                    Keyword.cluster_id == cluster.id,
                    func.lower(Keyword.keyword) == row.keyword.lower(),
                )
                .limit(1)
            )
            if existing is None:
                session.add(
                    Keyword(
                        cluster_id=cluster.id,
                        keyword=row.keyword,
                        volume=row.volume,
                        kd=row.kd,
                        cpc=row.cpc,
                        intent=row.intent,
                        source=IMPORT_SOURCE,
                    )
                )
                sheet_result.created += 1
            else:
                existing.volume = row.volume
                existing.kd = row.kd
                existing.cpc = row.cpc
                existing.intent = row.intent
                sheet_result.updated += 1

        report.sheets.append(sheet_result)
        if cluster_created:
            report.clusters_created += 1
        report.keywords_created += sheet_result.created
        report.keywords_updated += sheet_result.updated

    session.commit()
    logger.info(
        "keyword_import_done",
        extra={
            "event": "keyword_import_done",
            "file": report.file_name,
            "clusters_created": report.clusters_created,
            "keywords_created": report.keywords_created,
            "keywords_updated": report.keywords_updated,
            "warning_count": len(report.warnings),
        },
    )
    return report
