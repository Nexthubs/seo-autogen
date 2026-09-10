"""Step: article writer (SEO-AUTO-DEV-SPEC.md sections 24, 25).

Input (section 25 / 50) — the Writer receives ONLY:

    SEO Guideline, Content Brief, Validated Outline, SERP Synthesis,
    Evidence Notes, Allowed Internal Link Markers

Never the 5 full competitor sources (section 25: lowers plagiarism
risk, context load, structural imitation, attention dilution).

Checkpoint (section 9): persist the draft as article version 1
(section 28: never overwritten later) and commit.
"""

import json
import logging
import re

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.pipeline.steps._article_common import (
    build_article_view,  # noqa: F401  (re-exported for tests)
    load_research_context,
    persist_article_version,
)
from app.pipeline.steps._common import llm_model_name, set_llm_prompt
from app.providers.llm.base import LLMProvider
from app.schemas.article import ArticleDocument, ArticleDraftOutput
from app.services.article_renderer import normalize_slug
from app.services.prompt_service import PromptSpec, load_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "article_writer"

#: ``# <text>`` at line start, but NOT ``##``.
_H1_LINE = re.compile(r"(?m)^#\s(?!\s)(.*)$")


def strip_h1(body_markdown: str) -> str:
    """Lenient H1 removal: the local model occasionally emits an H1 in
    the body despite the prompt. ``# X`` becomes ``## X`` so the
    document stays valid (section 5 hard rule)."""
    return _H1_LINE.sub(r"## \1", body_markdown)


def build_user_prompt(
    guideline: str,
    brief: dict,
    outline: dict,
    synthesis: dict,
    evidence: list[dict],
    markers: list[str],
) -> str:
    """Assemble the Writer user prompt (section 50)."""
    parts = [
        "SEO Guideline:\n" + guideline,
        "Content Brief:\n" + json.dumps(brief, ensure_ascii=False, indent=1),
        "Validated Outline (follow it exactly, in order):\n"
        + json.dumps(outline, ensure_ascii=False, indent=1),
        "SERP Synthesis:\n" + json.dumps(synthesis, ensure_ascii=False, indent=1),
        "Evidence Notes (the ONLY authorized facts):\n"
        + json.dumps(evidence, ensure_ascii=False, indent=1),
        "Allowed internal link markers (use ONLY these, verbatim):\n"
        + (", ".join(markers) if markers else "(none)"),
        "\nWrite the complete article now.",
    ]
    return "\n\n".join(parts)


def build_article_document(
    raw: ArticleDraftOutput | dict,
    *,
    outline_title: str,
    brief: dict,
    target_function: str | None,
) -> ArticleDocument:
    """Assemble the validated ArticleDocument from the writer's JSON
    (sections 24, 6.2).

    Program-side hard rules (not left to the LLM):
    - title = the validated outline title (section 6.1: title flows
      Brief -> Outline -> Writer -> Final);
    - body_markdown is H1-stripped (section 5);
    - slug = normalize_slug(LLM suggestion, title) (section 6.2).
    """
    # ``raw`` is either the validated ArticleDraftOutput or a plain dict.
    _get = (
        (lambda k, d="": getattr(raw, k, d))
        if not isinstance(raw, dict)
        else (lambda k, d="": raw.get(k, d))
    )
    title = (outline_title or "").strip()
    body = strip_h1(_get("body_markdown") or "")
    doc = ArticleDocument(
        title=title,
        body_markdown=body,
        seo_title=(_get("seo_title") or title).strip(),
        meta_description=(_get("meta_description") or "").strip(),
        slug=normalize_slug(_get("slug"), title),
        primary_keyword=brief.get("primary_keyword", ""),
        secondary_keywords=list(brief.get("secondary_keywords", [])),
        long_tail_keywords=list(brief.get("long_tail_keywords", [])),
        search_intent=brief.get("search_intent", ""),
        article_strategy=brief.get("article_strategy", ""),
        target_function=target_function,
    )
    return doc


async def run_article_writer(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    guideline_excerpt: str,
    prompt: PromptSpec | None = None,
) -> ArticleDocument:
    """Generate the article draft (v1) and persist it as a version."""
    prompt = prompt or load_prompt(PROMPT_NAME)

    job.status = JobStatus.ARTICLE_GENERATING.value
    job.current_step = "article_generating"
    session.flush()

    ctx = load_research_context(session, job)
    if ctx["brief"] is None:
        from app.core.exceptions import ErrorCode, PipelineError

        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            "cannot write an article without a content brief",
        )
    if ctx["outline"] is None:
        from app.core.exceptions import ErrorCode, PipelineError

        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            "cannot write an article without a validated outline",
        )

    set_llm_prompt(llm, prompt)
    raw: ArticleDraftOutput = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_user_prompt(
            guideline_excerpt,
            ctx["brief"],
            ctx["outline"],
            ctx["synthesis"],
            ctx["evidence"],
            ctx["markers"],
        ),
        response_model=ArticleDraftOutput,
        temperature=0.7,
    )

    doc = build_article_document(
        raw,
        outline_title=ctx["outline"]["title"],
        brief=ctx["brief"],
        target_function=job.target_function,
    )

    persist_article_version(
        session,
        job,
        doc,
        stage="writer",
        model=llm_model_name(llm),
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        prompt_hash=prompt.prompt_hash,
    )

    job.status = JobStatus.SEO_REVIEWING.value
    job.current_step = "seo_reviewing"
    session.commit()

    logger.info(
        "article_writer_done",
        extra={
            "event": "article_writer_done",
            "job_id": str(job.id),
            "words": len(doc.body_markdown.split()),
            "prompt_version": prompt.version,
        },
    )
    return doc
