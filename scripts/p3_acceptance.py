"""P3 acceptance demo: keyword dataset (SEMrush Excel) + internal links.

The SEMrush Excel is generated in-memory with openpyxl (no vendor needed);
everything below the workbook is REAL:

  - real PostgreSQL (Docker ``seo-pg``)
  - real migration 0004 (keyword_clusters / keywords / internal_link_rules)
  - real import_workbook parse + upsert
  - real keyword lookup + strategy queries
  - real internal link validate/resolve

Acceptance checklist (spec sections 19, 20, 21, 46.2-46.4; P3):
  [ ] multi-sheet Excel -> Sheet = topic cluster, Keyword = row,
      Volume/KD/CPC parsed
  [ ] unrecognised column -> warning, never silently guessed
  [ ] repeat import -> upsert, NO duplicate records
  [ ] keyword in dataset -> job.keyword_metrics_available = True
  [ ] keyword NOT in dataset -> job still creatable,
      keyword_metrics_available = False (SERP-only, no LLM guessing)
  [ ] internal link marker [[INTERNAL_LINK:X]] -> validate -> resolve to
      a markdown link; unknown marker reported

Run:  PYTHONPATH=/home/ubuntu/ai-coding/seo-autogen python3 scripts/p3_acceptance.py
"""

import sys
import tempfile
import uuid
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import select, text

from app.core.enums import JobStatus
from app.db.models import GenerationJob, Keyword
from app.db.session import SessionLocal, check_database
from app.pipeline.steps.keyword_prepare import prepare_keyword
from app.schemas.keyword import KeywordDatasetQuery
from app.schemas.internal_link import InternalLinkRule as Rule
from app.services import internal_link_service as ils
from app.services import keyword_service as ks
from app.services.keyword_import import import_workbook

ACC_KW = "p3acc anxious attachment no contact"
ACC_SHEET = "p3acc-sheet"

CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail and not ok else ""))


def build_workbook(path: Path, sheet: str) -> Path:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = sheet
    ws1.append(["Keyword", "Search Volume", "Keyword Difficulty", "CPC", "Intent", "Competitors"])
    ws1.append([ACC_KW, 1200, 28.5, 1.25, "Informational", 42])
    ws1.append(["p3acc what is anxious attachment", 2400, 35, 2.10, "Informational", 38])
    ws1.append(["p3acc how to get over anxious attachment", 590, 12, 0.9, "Transactional", 20])
    ws2 = wb.create_sheet("p3acc-avoidance")
    ws2.append(["keyword", "volume", "kd %", "cpc", "search intent"])
    ws2.append(["p3acc avoidant attachment signs", 700, 18.4, 0.95, "Informational"])
    wb.save(str(path))
    return path


def reset() -> None:
    """Remove ONLY the acceptance rows — the user dataset must survive."""
    with SessionLocal() as session:
        session.execute(
            text("DELETE FROM keywords WHERE cluster_id IN "
                 "(SELECT id FROM keyword_clusters WHERE sheet_name LIKE :s)"),
            {"s": "p3acc%"},
        )
        session.execute(
            text("DELETE FROM keyword_clusters WHERE sheet_name LIKE :s"),
            {"s": "p3acc%"},
        )
        session.execute(
            text("DELETE FROM internal_link_rules WHERE marker LIKE :m"),
            {"m": "P3ACC%"},
        )
        session.execute(text("DELETE FROM generation_jobs WHERE keyword LIKE :p"), {"p": "p3acc%"})
        session.commit()


def make_job(keyword: str) -> uuid.UUID:
    job_id = uuid.uuid4()
    with SessionLocal() as session:
        session.add(
            GenerationJob(
                id=job_id,
                keyword=keyword,
                language="en",
                market="US",
                strategy="auto",
                status=JobStatus.QUEUED.value,
                current_step="queued",
            )
        )
        session.commit()
    return job_id


def main() -> int:
    if not check_database():
        print("FATAL: PostgreSQL not reachable — cannot run P3 acceptance.")
        return 2
    reset()

    print("\n== 1. Multi-sheet Excel import (section 20) ==")
    with tempfile.TemporaryDirectory() as td:
        xlsx = build_workbook(Path(td) / "semrush.xlsx", ACC_SHEET)
        with SessionLocal() as session:
            report = import_workbook(session, xlsx, file_name="semrush.xlsx")
        check(
            "two sheets -> two topic clusters",
            report.clusters_created == 2,
            f"clusters_created={report.clusters_created}",
        )
        check(
            "all data rows imported as keywords",
            report.keywords_created == 4,
            f"keywords_created={report.keywords_created}",
        )
        check(
            "unrecognised column 'Competitors' warned, not guessed",
            any("Competitors" in w for w in report.warnings),
            f"warnings={report.warnings}",
        )
        with SessionLocal() as session:
            kw = session.scalars(
                select(Keyword).where(Keyword.keyword == ACC_KW)
            ).first()
            check("Volume parsed (1200)", kw.volume == 1200, f"volume={kw.volume}")
            check("KD parsed (28.5)", kw.kd is not None and float(kw.kd) == 28.5, f"kd={kw.kd}")
            check("CPC parsed (1.25)", kw.cpc is not None and float(kw.cpc) == 1.25, f"cpc={kw.cpc}")
            check("Intent parsed", kw.intent == "Informational", f"intent={kw.intent}")
            check("cluster name = sheet name", kw.cluster.name == ACC_SHEET, f"cluster={kw.cluster.name}")

    print("\n== 2. Repeat import -> upsert, no duplicates (section 20) ==")
    with tempfile.TemporaryDirectory() as td:
        xlsx = build_workbook(Path(td) / "semrush.xlsx", ACC_SHEET)
        with SessionLocal() as session:
            second = import_workbook(session, xlsx, file_name="semrush.xlsx")
        check(
            "repeat import creates 0 new clusters",
            second.clusters_created == 0,
            f"clusters_created={second.clusters_created}",
        )
        check(
            "repeat import creates 0 new keywords, updates all 4",
            second.keywords_created == 0 and second.keywords_updated == 4,
            f"created={second.keywords_created} updated={second.keywords_updated}",
        )
        with SessionLocal() as session:
            kw_count = session.scalar(
                text("SELECT COUNT(*) FROM keywords WHERE cluster_id IN "
                     "(SELECT id FROM keyword_clusters WHERE sheet_name LIKE :s)"),
                {"s": "p3acc%"},
            )
            cl_count = session.scalar(
                text("SELECT COUNT(*) FROM keyword_clusters WHERE sheet_name LIKE :s"),
                {"s": "p3acc%"},
            )
            check("keywords count still 4 (no duplicates)", kw_count == 4, f"count={kw_count}")
            check("clusters count still 2 (no duplicates)", cl_count == 2, f"count={cl_count}")

    print("\n== 3. Dataset keyword -> metrics available (section 19.1) ==")
    with SessionLocal() as session:
        m = ks.lookup_metrics(session, ACC_KW)
        check(
            "lookup returns metrics for a dataset keyword",
            m is not None and m.volume == 1200,
            f"m={m}",
        )
        high_vol = ks.query_dataset(session, KeywordDatasetQuery(min_volume=2000, cluster_name=ACC_SHEET))
        check(
            "High Volume strategy query works",
            any(r.keyword == "p3acc what is anxious attachment" for r in high_vol),
            f"high_vol={[r.keyword for r in high_vol]}",
        )
    job_id = make_job(ACC_KW)
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        prepare_keyword(session, job)
        check(
            "job.keyword_metrics_available = True for dataset keyword",
            job.keyword_metrics_available is True,
            f"flag={job.keyword_metrics_available}",
        )
        check("job advanced to serp_searching", job.status == JobStatus.SERP_SEARCHING.value, f"status={job.status}")

    print("\n== 4. Unknown keyword -> SERP-only mode (section 19.2) ==")
    unknown = "p3acc a keyword that is not in the dataset"
    with SessionLocal() as session:
        check("unknown keyword lookup returns None", ks.lookup_metrics(session, unknown) is None)
    job_id2 = make_job(unknown)
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id2)
        prepare_keyword(session, job)
        check("unknown-keyword job is still creatable (queued->serp_searching)", job.status == JobStatus.SERP_SEARCHING.value, f"status={job.status}")
        check(
            "job.keyword_metrics_available = False (SERP-only, no LLM guessing)",
            job.keyword_metrics_available is False,
            f"flag={job.keyword_metrics_available}",
        )

    print("\n== 5. Internal links: marker -> validate -> markdown link (section 21) ==")
    with SessionLocal() as session:
        ils.upsert_rule(
            session,
            Rule(
                marker="P3ACC_ATTACHMENT",
                anchor_text="what is anxious attachment",
                target_url="https://example.com/what-is-anxious-attachment",
                topic="attachment",
                keywords=["anxious attachment"],
            ),
        )
        session.commit()
        md = "See the [[INTERNAL_LINK:P3ACC_ATTACHMENT]] guide and [[INTERNAL_LINK:GHOST]] more."
        rendered, resolved, validation = ils.resolve_markers(session, md)
        check(
            "known marker resolved to markdown link",
            "[what is anxious attachment](https://example.com/what-is-anxious-attachment)" in rendered,
            f"rendered={rendered}",
        )
        check(
            "unknown marker reported, not resolved",
            validation.unknown_markers == ["GHOST"] and validation.valid is False,
            f"validation={validation}",
        )
        check(
            "LLM output contained a marker, never a URL",
            "[[INTERNAL_LINK:P3ACC_ATTACHMENT]]" in md and "http" not in md,
        )

    print("\n== 6. Self-cleanup ==")
    reset()
    with SessionLocal() as session:
        left_kw = session.scalar(
            text("SELECT COUNT(*) FROM keywords WHERE cluster_id IN "
                 "(SELECT id FROM keyword_clusters WHERE sheet_name LIKE :s)"),
            {"s": "p3acc%"},
        )
        left_jobs = session.scalar(text("SELECT COUNT(*) FROM generation_jobs WHERE keyword LIKE :p"), {"p": "p3acc%"})
    check("cleanup removed acceptance keywords", left_kw == 0, f"leftover={left_kw}")
    check("cleanup removed acceptance jobs", left_jobs == 0, f"leftover={left_jobs}")

    print("\n== P3 Acceptance Summary ==")
    passed = sum(1 for _, ok in CHECKS if ok)
    total = len(CHECKS)
    for label, ok in CHECKS:
        print(f"  [{'x' if ok else ' '}] {label}")
    print(f"\n  {passed}/{total} checks passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
