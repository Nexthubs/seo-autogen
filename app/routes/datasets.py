"""Keyword dataset routes (spec 44: ``/api/datasets/keywords*``, 43.5-adjacent).

``GET /api/datasets/keywords`` applies the strategy filter (section 19.1) and
returns the matching metrics; ``POST /api/datasets/keywords/import`` accepts an
``.xlsx`` workbook and imports it (one sheet per cluster, section 20).
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.keyword import KeywordDatasetQuery
from app.services.keyword_import import import_workbook
from app.services.keyword_service import query_dataset

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _strategy_query(strategy: str | None) -> KeywordDatasetQuery:
    """Map the 43.1 strategy names to a dataset query (section 19.1)."""
    s = (strategy or "auto").strip().lower()
    if s in ("", "auto"):
        return KeywordDatasetQuery()
    if s == "high_volume":
        return KeywordDatasetQuery(min_volume=1000, limit=200)
    if s == "low_kd":
        return KeywordDatasetQuery(max_kd=20, limit=200)
    if s == "high_cpc":
        return KeywordDatasetQuery(min_cpc=Decimal("1.0"), limit=200)
    if s == "long_tail":
        return KeywordDatasetQuery(long_tail_only=True, limit=200)
    # Pillar has no dataset filter (section 19.1: pillar is a strategy, not
    # a metric query) — return the broad set.
    return KeywordDatasetQuery(limit=200)


@router.get("/keywords")
def api_list_keywords(
    strategy: str = "auto",
    cluster: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_db),
) -> dict:
    query = _strategy_query(strategy)
    query.cluster_name = cluster or query.cluster_name
    query.limit = min(max(limit, 1), 500)
    rows = query_dataset(session, query)
    return {
        "strategy": strategy,
        "count": len(rows),
        "keywords": [
            {
                "keyword": r.keyword,
                "cluster": r.cluster_name,
                "volume": r.volume,
                "kd": float(r.kd) if r.kd is not None else None,
                "cpc": float(r.cpc) if r.cpc is not None else None,
                "intent": r.intent,
            }
            for r in rows
        ],
    }


@router.post("/keywords/import")
async def api_import_keywords(
    file: UploadFile = File(...), session: Session = Depends(get_db)
) -> dict:
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400, detail="only .xlsx workbooks are accepted"
        )
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="workbook too large")
    # The importer reads through openpyxl (path-based), so spool the upload.
    with tempfile.NamedTemporaryFile(
        suffix=".xlsx", delete=False
    ) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        report = import_workbook(session, tmp_path, file_name=file.filename)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        tmp_path.unlink(missing_ok=True)
    return {
        "file_name": report.file_name,
        "sheets": [
            {
                "sheet_name": s.sheet_name,
                "cluster_name": s.cluster_name,
                "created": s.created,
                "updated": s.updated,
                "skipped": s.skipped,
                "warnings": s.warnings,
            }
            for s in report.sheets
        ],
        "clusters_created": report.clusters_created,
        "keywords_created": report.keywords_created,
        "keywords_updated": report.keywords_updated,
        "keywords_skipped": report.keywords_skipped,
        "warnings": report.warnings,
    }
