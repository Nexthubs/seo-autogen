"""SQLAlchemy ORM models.

Models land with their owning phase
(P1 core, P2 SERP/source, P3 keywords, P4+ research/article).
Import all models here so Alembic autogenerate and the Base registry
see the complete metadata.
"""

from app.db.base import Base  # noqa: F401
from app.db.models.article import (
    ArticleReviewRow,
    ArticleVersionRow,
)  # noqa: F401
from app.db.models.images import ImageRow  # noqa: F401
from app.db.models.internal_link import InternalLinkRule  # noqa: F401
from app.db.models.job import GenerationJob  # noqa: F401
from app.db.models.keyword import Keyword, KeywordCluster  # noqa: F401
from app.db.models.llm_usage import LLMUsageRow  # noqa: F401
from app.db.models.research import (
    ArticleOutlineRow,
    CompetitorAnalysisRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)  # noqa: F401
from app.db.models.serp import SerpResult, SerpRun  # noqa: F401
from app.db.models.source import JobSource, SourcePage  # noqa: F401
from app.db.models.strapi_syncs import StrapiSyncRow  # noqa: F401

__all__ = [
    "ArticleOutlineRow",
    "ArticleReviewRow",
    "ArticleVersionRow",
    "Base",
    "CompetitorAnalysisRow",
    "ContentBriefRow",
    "EvidenceNoteRow",
    "GenerationJob",
    "InternalLinkRule",
    "JobSource",
    "Keyword",
    "KeywordCluster",
    "LLMUsageRow",
    "SerpResult",
    "SerpRun",
    "SerpSynthesisRow",
    "SourcePage",
    "StrapiSyncRow",
]
