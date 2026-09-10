"""P2 Top-5 unique competitor URL selection tests
(spec sections 12.1, 14, 14.1)."""

from app.pipeline.steps.serp_search import select_top5_unique
from app.schemas.serp import OrganicResult, SERPResponse


def _resp(urls: list[str], rank_offset: int = 1) -> SERPResponse:
    organic = [
        OrganicResult(
            rank=rank_offset + i,
            title=f"T{i}",
            url=url,
        )
        for i, url in enumerate(urls)
    ]
    return SERPResponse(keyword="k", organic_results=organic)


def test_picks_top5_in_rank_order():
    urls = [f"https://s{i}.example.com/a{i}" for i in range(1, 9)]
    picked = select_top5_unique(_resp(urls))
    assert len(picked) == 5
    assert picked == [(i + 1, urls[i]) for i in range(5)]


def test_dedupes_by_normalized_url_not_llm():
    urls = [
        "https://a.example.com/x?utm_source=google",  # rank 1
        "https://a.example.com/x",  # rank 2, same normalized URL
        "https://b.example.com/y",  # rank 3
    ]
    picked = select_top5_unique(_resp(urls))
    assert picked == [(1, "https://a.example.com/x"), (3, "https://b.example.com/y")]


def test_skips_existing_urls_for_backfill():
    """A duplicate-content URL already seen -> next organic backfills."""
    urls = [
        "https://a.example.com/x",
        "https://b.example.com/y",
        "https://c.example.com/z",
    ]
    picked = select_top5_unique(
        _resp(urls), existing_urls={"https://a.example.com/x"}
    )
    assert [u for _, u in picked] == ["https://b.example.com/y", "https://c.example.com/z"]


def test_fewer_than_5_available():
    urls = ["https://a.example.com/x", "https://b.example.com/y"]
    picked = select_top5_unique(_resp(urls))
    assert len(picked) == 2


def test_empty_url_entries_skipped():
    urls = ["https://a.example.com/x", "", "https://b.example.com/y"]
    picked = select_top5_unique(_resp(urls))
    assert [u for _, u in picked] == ["https://a.example.com/x", "https://b.example.com/y"]


def test_hostname_case_and_fragment_variants_collapse():
    # Only the hostname is lowercased (spec 14); same path with different
    # host casing + fragment is the same normalized URL.
    urls = [
        "HTTPS://A.EXAMPLE.COM/Path#frag",  # rank 1
        "https://a.example.com/Path",  # rank 2, duplicate
        "https://b.example.com/y",  # rank 3
    ]
    picked = select_top5_unique(_resp(urls))
    assert [u for _, u in picked] == [
        "https://a.example.com/Path",
        "https://b.example.com/y",
    ]
