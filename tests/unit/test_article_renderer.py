"""P5 unit: slug normalization + article.md renderer
(SEO-AUTO-DEV-SPEC.md sections 5, 6.2).
"""

from app.services.article_renderer import (
    MAX_SLUG_CHARS,
    disambiguate_slug,
    export_article,
    normalize_slug,
    render_article_markdown,
    slug_is_unique,
)


def test_normalize_slug_basic():
    assert normalize_slug("Anxious Attachment No Contact") == "anxious-attachment-no-contact"


def test_normalize_slug_lowercases_and_kebab():
    assert normalize_slug("My-ARTICLE--Second  Part??") == "my-article-second-part"


def test_normalize_slug_transliterates_unicode():
    assert normalize_slug("  Café  au lait!! ") == "cafe-au-lait"


def test_normalize_slug_dedupes_hyphens_and_trims():
    assert normalize_slug("--a---b--") == "a-b"


def test_normalize_slug_falls_back_to_title():
    assert (
        normalize_slug("", "Anxious Attachment No Contact")
        == "anxious-attachment-no-contact"
    )
    assert (
        normalize_slug("   ", "Some Fallback Title")
        == "some-fallback-title"
    )


def test_normalize_slug_caps_length():
    long = "x" * (MAX_SLUG_CHARS + 10)
    assert len(normalize_slug(long)) == MAX_SLUG_CHARS
    assert not normalize_slug(long).endswith("-")


def test_slug_is_unique():
    assert slug_is_unique({"a", "b"}, "c") is True
    assert slug_is_unique({"a", "b"}, "a") is False


def test_disambiguate_slug():
    assert disambiguate_slug("slug", set()) == "slug"
    assert disambiguate_slug("slug", {"slug"}) == "slug-2"
    assert disambiguate_slug("slug", {"slug", "slug-2"}) == "slug-3"
    assert disambiguate_slug("slug", {"slug", "slug-2", "slug-3", "slug-4"}) == "slug-5"


def test_render_has_exactly_one_h1():
    md = render_article_markdown(
        title="The Title",
        body_markdown="## Section A\n\nBody text.\n\n## FAQ\n\nQ and A.",
        seo_title="SEO Title",
        meta_description="A meta description.",
        slug="the-title",
    )
    h1_lines = [
        l for l in md.splitlines()
        if l.startswith("# ") or l == "#"
    ]
    assert h1_lines == ["# The Title"]
    assert "## Section A" in md and "## FAQ" in md


def test_render_front_matter():
    md = render_article_markdown(
        title="T",
        body_markdown="## S\n\nBody",
        seo_title="ST",
        meta_description="MD",
        slug="t",
    )
    lines = md.splitlines()
    assert lines[0] == "---"
    assert lines[1] == "metaTitle: ST"
    assert lines[2] == "metaDescription: MD"
    assert lines[3] == "slug: t"
    assert lines[4] == "---"
    assert lines[5] == ""
    assert lines[6] == "# T"


def test_export_article_writes_file(tmp_path):
    out = export_article(
        tmp_path / "nested" / "article.md",
        title="T",
        body_markdown="## S\n\nBody",
        seo_title="ST",
        meta_description="MD",
        slug="t",
    )
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "# T\n" in text
    assert str(out) == str(tmp_path / "nested" / "article.md")
