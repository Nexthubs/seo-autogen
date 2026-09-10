"""P2 acceptance demo: "anxious attachment no contact".

DataForSEO / Exa credentials are not configured in this environment, so the
two HTTP providers are driven through httpx.MockTransport with realistic
payloads. Everything below the provider boundary is REAL:

  - real PostgreSQL (Docker ``seo-pg``)
  - real migrations (serp_runs / serp_results / source_pages / job_sources)
  - real run_serp_search + run_source_extract pipeline steps
  - real URL normalization, Top-5 selection, source cache TTL,
    content-hash dedup

Acceptance checklist (spec sections 12, 12.1, 12.2, 14, 14.1, 14.2, 15):
  [ ] DataForSEO request "success" (HTTP 200, parsed organic list)
  [ ] organic results persisted to serp_results
  [ ] Top-5 unique competitor URLs selected
  [ ] PAA persisted (present in this fixture)
  [ ] competitor content extracted to source_pages
  [ ] second run of the same URLs within TTL -> cache hit, 0 extractor calls

Run:  python3 scripts/p2_acceptance.py
"""

import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete as sa_delete, select

from app.core.config import Settings
from app.db.models import GenerationJob
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.db.session import SessionLocal, check_database
from app.pipeline.steps.serp_search import run_serp_search, select_top5_unique
from app.pipeline.steps.source_extract import run_source_extract
from app.providers.extractor.exa import ExaContentExtractor
from app.providers.serp.dataforseo import DataForSEOSERPProvider
from app.services.source_cache import SourceCache

KEYWORD = "anxious attachment no contact"

# A realistic Google-organic SERP for the acceptance keyword.
ORGANIC = [
    (1, "Anxious Attachment and No Contact: What Happens", "https://www.psychologytoday.com/us/blog/what-anxious-people-need/no-contact"),
    (2, "Why No Contact Works for Anxious Attachment", "https://www.thriveglobal.com/stories/why-no-contact-works"),
    (3, "No Contact Rules for Anxious Attachers", "https://www.mindbodygreen.com/no-contact-anxious-attachment"),
    (4, "Healing Anxious Attachment: A Practical Guide", "https://www.thestandard.co/healing-anxious-attachment"),
    (5, "Attachment Styles and the No-Contact Strategy", "https://www.psypost.org/attachment-no-contact"),
    (6, "Anxious Attachment: How to Stop Chasing", "https://www.healthline.com/anxious-attachment-chasing"),
    (7, "The Science of No Contact", "https://www.scientificamerican.com/science-no-contact"),
    (8, "Rebuilding After Anxious Attachment", "https://www.nytimes.com/rebuilding-after-anxious-attachment"),
]
PAA = [
    "Why does no contact work for anxious attachment?",
    "How long should you do no contact?",
    "What is anxious attachment in relationships?",
]
RELATED = ["no contact self respect", "anxious attachment style examples"]

# URL #4 (thestandard.co) serves the SAME article body as URL #5 (psypost)
# to exercise the duplicate-content backfill path (spec 14.1): the first
# occurrence (rank 4) is kept, rank 5 is dropped, and rank 6 backfills the
# Top-5 slot.
DUPLICATE_URL = "https://www.thestandard.co/healing-anxious-attachment"
DUPLICATE_OF = "https://www.psypost.org/attachment-no-contact"
BACKFILL_URL = "https://www.healthline.com/anxious-attachment-chasing"


def _content_for(url: str) -> str:
    if url == DUPLICATE_URL:
        base = DUPLICATE_OF
    else:
        base = url
    return (
        f"Competitor content for {base}: evidence-based advice on how no "
        "contact helps the anxious attacher's nervous system reset and how "
        "to rebuild self trust without chasing."
    )


def _serp_payload() -> dict:
    """Official DataForSEO envelope (machine-verified contract).

    ``tasks[0].result`` is a LIST of SERP blocks; the single block's
    ``items[]`` is a FLAT, MIXED-type list. Organic items carry
    ``rank_group`` (the rank), PAA items nest ``people_also_ask_element``
    dicts (question = element ``title``, url from ``expanded_element``),
    related items hold a list of plain strings, and a featured_snippet
    item carries its own top-level title/url/description.
    """
    items: list[dict] = []
    for rank, title, url in ORGANIC:
        items.append(
            {
                "type": "organic",
                "rank_group": rank,
                "rank_absolute": rank + 4,
                "page": 1,
                "position": "left",
                "domain": url.split("/")[2],
                "title": title,
                "url": url,
                "description": f"snippet for {title}",
            }
        )
    items.append(
        {
            "type": "people_also_ask",
            "rank_group": 1,
            "items": [
                {
                    "type": "people_also_ask_element",
                    "title": q,
                    "expanded_element": [
                        {
                            "type": "people_also_ask_expanded_element",
                            "url": f"https://paa.example.com/{i}",
                            "domain": "paa.example.com",
                            "title": f"Answer to: {q}",
                            "description": "snippet",
                        }
                    ],
                }
                for i, q in enumerate(PAA)
            ],
        }
    )
    items.append({"type": "related_searches", "rank_group": 1, "items": RELATED})
    items.append(
        {
            "type": "featured_snippet",
            "rank_group": 1,
            "domain": "featured.example.com",
            "title": "Featured: What is no contact?",
            "featured_title": None,
            "description": "No contact is the practice of cutting off all communication.",
            "url": "https://featured.example.com/no-contact",
        }
    )
    return {
        "version": "2026-09-01",
        "status_code": 20000,
        "status_message": "OK",
        "time": "0.24",
        "cost": 0.03,
        "tasks": [
            {
                "cost": 0.03,
                "status_code": 20000,
                "status_message": "OK",
                "id": "task-acceptance",
                "path": "/v3/serp/google/organic/live/advanced",
                "result_count": 1,
                "time": "0.2",
                "data": {},
                "result": [
                    {
                        "type": "organic",
                        "keyword": KEYWORD,
                        "items_count": len(items),
                        "items": items,
                    }
                ],
            }
        ],
    }


def _exa_payload(urls: list[str]) -> dict:
    return {
        "results": [
            {
                "id": u,
                "title": f"Page for {u}",
                "url": u,
                "text": _content_for(u),
                "publishedDate": "2024-05-01",
            }
            for u in urls
        ],
        "statuses": [
            {"id": u, "status": "success", "source": "cached"} for u in urls
        ],
        "costDollars": {"total": 0.003 * len(urls)},
    }


def _build_providers(
    settings: Settings,
) -> tuple[DataForSEOSERPProvider, ExaContentExtractor, list]:
    """Two REAL providers, each backed by a MockTransport.

    ``exa_calls`` records every extractor request so the cache-hit
    assertion (0 extractor calls on the second run) is airtight.
    """
    exa_calls: list[list[str]] = []

    def serp_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_serp_payload())

    def exa_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        exa_calls.append(body["ids"])
        return httpx.Response(200, json=_exa_payload(body["ids"]))

    serp = DataForSEOSERPProvider(
        settings=settings,
        client=httpx.AsyncClient(
            base_url=settings.dataforseo_base_url,
            transport=httpx.MockTransport(serp_handler),
            auth=("acceptance", "acceptance"),
        ),
    )
    exa = ExaContentExtractor(
        settings=settings,
        client=httpx.AsyncClient(
            base_url=settings.exa_base_url,
            transport=httpx.MockTransport(exa_handler),
            headers={"x-api-key": "acceptance-key"},
        ),
    )
    return serp, exa, exa_calls


async def main() -> int:
    if not check_database():
        print("SKIP: PostgreSQL not reachable", file=sys.stderr)
        return 2

    settings = Settings(_env_file=None)
    serp, exa, exa_calls = _build_providers(settings)
    cache = SourceCache(settings)

    with SessionLocal() as session:
        job = GenerationJob(keyword=KEYWORD, status="queued")
        session.add(job)
        session.commit()
        job_id = job.id

    print(f"job_id  = {job_id}")
    print(f"keyword = {KEYWORD!r}")
    print()

    ok = True

    # ---- Step 1: SERP search (spec 12, 12.1, 12.2) ----------
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        response = await run_serp_search(session, job, serp, settings=settings)

    top5 = select_top5_unique(response)
    print("[1] DataForSEO request: SUCCESS (HTTP 200, organic parsed)")
    print(f"    organic results returned : {len(response.organic_results)}")
    print(f"    PAA questions            : {len(response.paa_questions)}")
    print(f"    related searches         : {len(response.related_searches)}")
    print(f"    featured snippet present : {response.featured_snippet is not None}")
    print("    Top-5 unique competitor URLs:")
    for rank, url in top5:
        print(f"        rank {rank}: {url}")
    print()

    with SessionLocal() as session:
        run = session.scalar(select(SerpRun).where(SerpRun.job_id == job_id))
        n_org = len(
            session.scalars(
                select(SerpResult).where(
                    SerpResult.serp_run_id == run.id,
                    SerpResult.result_type == "organic",
                )
            ).all()
        )
        n_paa = len(
            session.scalars(
                select(SerpResult).where(
                    SerpResult.serp_run_id == run.id,
                    SerpResult.result_type == "paa",
                )
            ).all()
        )
        n_feat = len(
            session.scalars(
                select(SerpResult).where(
                    SerpResult.serp_run_id == run.id,
                    SerpResult.result_type == "featured",
                )
            ).all()
        )
        raw = run.raw_response

    org_ok = n_org == len(ORGANIC)
    paa_ok = n_paa == len(PAA)
    feat_ok = n_feat == 1
    top5_ok = len(top5) == 5
    print(
        f"[2] persisted serp_results   : {n_org} organic, {n_paa} paa, "
        f"{n_feat} featured"
    )
    print(
        f"    serp_runs.raw_response   : status_code="
        f"{raw.get('status_code')}, tasks={len(raw.get('tasks', []))}"
    )
    print()

    # ---- Step 2: source extract (spec 14.1, 14.2, 15) -------
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        pages = await run_source_extract(
            session, job, exa, cache=cache, settings=settings
        )
    calls_after_first_run = len(exa_calls)

    page_urls = {p.normalized_url for p in pages}
    dup_ok = DUPLICATE_OF not in page_urls and DUPLICATE_URL in page_urls
    backfill_ok = BACKFILL_URL in page_urls
    print(f"[3] competitor content extracted: {len(pages)} pages")
    for p in pages:
        print(f"        {p.normalized_url}  ({len(p.content_markdown)} chars)")
    print(f"    extractor HTTP calls (run 1) : {calls_after_first_run}")
    print(f"    duplicate rank-5 URL dropped : {dup_ok}")
    print(f"    rank-6 backfill present      : {backfill_ok}")
    print()

    # ---- Step 3: re-run within TTL -> cache hit, no extractor ----
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        pages2 = await run_source_extract(
            session, job, exa, cache=cache, settings=settings
        )
    new_calls = len(exa_calls) - calls_after_first_run
    cache_ok = new_calls == 0 and len(pages2) == len(pages)

    print(f"[4] second run (same URLs, within {settings.source_cache_ttl_hours}h TTL):")
    print(f"        pages returned (from cache) : {len(pages2)}")
    print(f"        NEW extractor calls         : {new_calls}")
    print()

    # ---- DB row checks ---------------------------------------
    with SessionLocal() as session:
        src_rows = [
            r for r in session.scalars(select(SourcePage)).all()
            if r.url in {u for batch in exa_calls for u in batch}
        ]
        n_js = len(
            session.scalars(select(JobSource).where(JobSource.job_id == job_id)).all()
        )
        distinct = len({r.url_hash for r in src_rows}) == len(src_rows)
    print(f"[5] source_pages rows (fetched)   : {len(src_rows)}")
    print(f"    job_sources rows for job      : {n_js}")
    print(f"    url_hash unique per page      : {distinct}")
    print()

    # ---- cleanup -------------------------------------------------
    with SessionLocal() as session:
        all_urls = {u for batch in exa_calls for u in batch}
        sp_ids = session.scalars(
            select(SourcePage.id).where(SourcePage.url.in_(all_urls))
        ).all()
        session.execute(sa_delete(JobSource).where(JobSource.job_id == job_id))
        session.execute(
            sa_delete(SerpResult).where(
                SerpResult.serp_run_id.in_(
                    select(SerpRun.id).where(SerpRun.job_id == job_id)
                )
            )
        )
        session.execute(sa_delete(SerpRun).where(SerpRun.job_id == job_id))
        session.execute(sa_delete(SourcePage).where(SourcePage.id.in_(sp_ids)))
        session.delete(session.get(GenerationJob, job_id))
        session.commit()

    ok = (
        ok
        and org_ok
        and paa_ok
        and feat_ok
        and top5_ok
        and dup_ok
        and backfill_ok
        and cache_ok
    )
    print("=" * 62)
    print(f"ACCEPTANCE: {'PASS' if ok else 'FAIL'}")
    print(f"  [x] DataForSEO request success : {raw.get('status_code') == 20000}")
    print(f"  [x] organic persisted          : {org_ok}  ({n_org}/{len(ORGANIC)})")
    print(f"  [x] Top-5 unique URLs selected : {top5_ok}  (got {len(top5)})")
    print(f"  [x] PAA persisted              : {paa_ok}  ({n_paa}/{len(PAA)})")
    print(f"  [x] featured snippet persisted : {feat_ok}  ({n_feat}/1)")
    print(f"  [x] content extracted          : {len(pages) >= 1}  ({len(pages)} pages)")
    print(f"  [x] duplicate content deduped  : {dup_ok and backfill_ok}")
    print(f"  [x] 2nd-run cache hit, 0 calls : {cache_ok}")
    print("  [x] cleanup                      : done")
    print("=" * 62)

    await serp.aclose()
    await exa.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
