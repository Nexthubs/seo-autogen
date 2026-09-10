"""Externalized prompt loading (SEO-AUTO-DEV-SPEC.md sections 47, 48).

All long prompts live in ``prompts/*.md``; Python files must not contain
whole prompts. Each file starts with a small front-matter block:

    ---
    name: article_writer
    version: 1.0
    ---

At load time the content is hashed (SHA256) and the pipeline records
``prompt_name`` / ``prompt_version`` / ``prompt_hash`` per persisted
LLM result.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

#: Spec section 47: the P4 research prompts (later phases add more).
RESEARCH_PROMPT_NAMES = (
    "competitor_analyzer",
    "serp_synthesis",
    "evidence_research",
    "content_brief",
    "outline_generator",
    "outline_repair",
)

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

_cache: dict[str, "PromptSpec"] = {}


@dataclass(frozen=True)
class PromptSpec:
    """A loaded prompt with its identity metadata (section 48)."""

    name: str
    version: str
    content: str
    prompt_hash: str

    @property
    def path(self) -> Path:
        return _PROMPTS_DIR / f"{self.name}.md"


def _parse_front_matter(text: str) -> tuple[str, str, str]:
    """Split the ``name``/``version`` front matter from the body.

    Returns ``(name, version, content)``.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("prompt file must start with a '---' front matter block")
    meta: dict[str, str] = {}
    body_start = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = i + 1
            break
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    if body_start is None:
        raise ValueError("prompt file front matter block is not closed")
    name = meta.get("name", "")
    version = meta.get("version", "")
    if not name or not version:
        raise ValueError("prompt front matter requires 'name' and 'version'")
    content = "\n".join(lines[body_start:]).lstrip("\n")
    return name, version, content


def load_prompt(name: str) -> PromptSpec:
    """Load ``prompts/{name}.md`` (cached per process)."""
    if name in _cache:
        return _cache[name]
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")
    fname, version, content = _parse_front_matter(text)
    if fname != name:
        raise ValueError(
            f"prompt front matter name {fname!r} does not match file {name!r}"
        )
    spec = PromptSpec(
        name=name,
        version=version,
        content=content,
        prompt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    _cache[name] = spec
    return spec


def seo_guideline_excerpt(settings) -> str:
    """Load the SEO guideline (``prompts/seo_article_guideline.md``).

    The guideline is a content rulebook, not a versioned prompt: no
    front matter is required.
    """
    path = Path(settings.seo_guideline_path)
    if not path.is_absolute():
        path = _PROMPTS_DIR.parent / path
    return path.read_text(encoding="utf-8")


def brand_guideline_excerpt(settings) -> str:
    """Load the brand visual guideline
    (``prompts/brand_visual_guideline.md``).

    A rulebook, not a versioned prompt: no front matter required.
    Adding/replacing this file updates every image prompt without a
    Python change (spec section 32 / 47).
    """
    path = Path(settings.brand_visual_guideline_path)
    if not path.is_absolute():
        path = _PROMPTS_DIR.parent / path
    return path.read_text(encoding="utf-8")


def all_prompt_specs() -> list[PromptSpec]:
    """Load every versioned prompt (spec 48) in a stable order.

    A *versioned prompt* is a ``prompts/*.md`` file carrying a
    ``name``/``version`` front-matter block (section 47). The two rulebooks
    (``seo_article_guideline.md``, ``brand_visual_guideline.md``) have no
    front matter and are intentionally excluded: they are content rulebooks,
    not LLM result prompts.

    Returns the specs sorted by name so the dashboard is deterministic.
    """
    specs: list[PromptSpec] = []
    for path in sorted(_PROMPTS_DIR.glob("*.md")):
        if path.stem in _cache:
            specs.append(_cache[path.stem])
            continue
        try:
            specs.append(load_prompt(path.stem))
        except ValueError:
            # No (or malformed) front matter → rulebook, skip (section 47).
            continue
    return specs


def clear_cache() -> None:
    """Test helper: drop the load cache."""
    _cache.clear()
