"""P4 unit: externalized prompt loading (spec sections 47, 48)."""

import hashlib

import pytest

from app.services import prompt_service as ps


@pytest.fixture(autouse=True)
def _fresh_cache():
    ps.clear_cache()
    yield
    ps.clear_cache()


def test_all_research_prompts_load_with_identity(tmp_path):
    """Spec 47: every P4 prompt exists; spec 48: name + version + hash."""
    assert len(ps.RESEARCH_PROMPT_NAMES) == 6
    for name in ps.RESEARCH_PROMPT_NAMES:
        spec = ps.load_prompt(name)
        assert spec.name == name
        assert spec.version
        assert len(spec.prompt_hash) == 64  # sha256 hex
        text = spec.path.read_text(encoding="utf-8")
        assert spec.prompt_hash == hashlib.sha256(text.encode()).hexdigest()
        assert spec.content and not spec.content.startswith("---")


def test_prompt_body_does_not_leak_front_matter():
    spec = ps.load_prompt("competitor_analyzer")
    assert "version:" not in spec.content.splitlines()[0]
    assert spec.content.startswith("You are")


def test_load_prompt_missing_file():
    with pytest.raises(FileNotFoundError):
        ps.load_prompt("no_such_prompt_xyz")


def _write(tmp_path, name: str, text: str):
    """``name`` includes the ``.md`` extension."""
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def test_parse_front_matter_ok():
    name, version, content = ps._parse_front_matter(
        "---\nname: x\nversion: 2.1\n---\nbody here"
    )
    assert (name, version, content) == ("x", "2.1", "body here")


def test_parse_front_matter_requires_first_line():
    with pytest.raises(ValueError):
        ps._parse_front_matter("no front matter\n---\nname: x\nversion: 1\n---\nbody")


def test_parse_front_matter_unclosed_block():
    with pytest.raises(ValueError):
        ps._parse_front_matter("---\nname: x\nversion: 1\nno close")


def test_parse_front_matter_requires_name_and_version():
    with pytest.raises(ValueError):
        ps._parse_front_matter("---\nname: x\n---\nbody")
    with pytest.raises(ValueError):
        ps._parse_front_matter("---\nversion: 1\n---\nbody")


def test_name_must_match_filename(monkeypatch, tmp_path):
    _write(tmp_path, "my_prompt.md", "---\nname: other_name\nversion: 1.0\n---\nbody")
    monkeypatch.setattr(ps, "_PROMPTS_DIR", tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        ps.load_prompt("my_prompt")


def test_version_mismatch_is_recorded_not_rejected(tmp_path, monkeypatch):
    """Spec 48 records the version from front matter (no external
    version file in P4 scope); the version is exposed on the spec."""
    _write(tmp_path, "vp.md", "---\nname: vp\nversion: 9.9\n---\nbody")
    monkeypatch.setattr(ps, "_PROMPTS_DIR", tmp_path)
    spec = ps.load_prompt("vp")
    assert spec.version == "9.9"


def test_cache_and_clear(monkeypatch, tmp_path):
    _write(tmp_path, "cp.md", "---\nname: cp\nversion: 1.0\n---\nbody one")
    monkeypatch.setattr(ps, "_PROMPTS_DIR", tmp_path)
    first = ps.load_prompt("cp")
    _write(tmp_path, "cp.md", "---\nname: cp\nversion: 2.0\n---\nbody two")
    cached = ps.load_prompt("cp")
    assert cached is first  # cached
    ps.clear_cache()
    fresh = ps.load_prompt("cp")
    assert fresh is not first
    assert fresh.version == "2.0"


def test_seo_guideline_excerpt_relative_path():
    """The guideline is a rulebook: no front matter required."""
    class _S:
        seo_guideline_path = "prompts/seo_article_guideline.md"

    excerpt = ps.seo_guideline_excerpt(_S())
    assert len(excerpt) > 100
    assert "guideline" in excerpt.lower() or "SEO" in excerpt
