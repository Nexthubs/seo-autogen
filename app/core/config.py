"""Application settings.

All URLs, API keys, model names and timeouts come from here (backed by
environment variables / .env). No provider-specific value may be hardcoded
in business code. See SEO-AUTO-DEV-SPEC.md section 11 for the canonical
.env layout.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ======================================================
    # APP
    # ======================================================
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_secret_key: str = "change-me"
    app_timezone: str = "UTC"

    data_dir: str = "/app/data"

    # ======================================================
    # DATABASE
    # ======================================================
    database_url: str = "postgresql+psycopg://seo:seo@postgres:5432/seo"

    # ======================================================
    # REDIS / RQ
    # ======================================================
    redis_url: str = "redis://redis:6379/0"
    rq_queue_name: str = "seo"

    # ======================================================
    # LLM
    # ======================================================
    llm_provider: str = "openai_compatible"

    llm_base_url: str = "http://host.docker.internal:1234/v1"
    llm_api_key: str = "local"
    llm_model: str = "qwen3.8-27b"

    llm_timeout_seconds: int = 300
    llm_max_retries: int = 2

    llm_temperature_analysis: float = 0.25
    llm_temperature_writing: float = 0.70
    llm_temperature_review: float = 0.20

    # ======================================================
    # DATAFORSEO
    # ======================================================
    serp_provider: str = "dataforseo"

    dataforseo_base_url: str = "https://api.dataforseo.com"
    dataforseo_login: str = ""
    dataforseo_password: str = ""

    dataforseo_location_code: int = 2840
    dataforseo_language_code: str = "en"
    dataforseo_device: str = "desktop"
    dataforseo_depth: int = 10

    dataforseo_paa_click_depth: int = 0
    dataforseo_load_async_ai_overview: bool = False
    dataforseo_calculate_rectangles: bool = False

    # P9-A: provider timeouts (spec section 61 — every timeout comes from
    # Settings/.env, nothing hardcoded in provider code).
    serp_timeout_seconds: int = 120

    # ======================================================
    # EXTRACTOR
    # ======================================================
    content_extractor_provider: str = "exa"

    exa_base_url: str = "https://api.exa.ai"
    exa_api_key: str = ""

    tavily_base_url: str = "https://api.tavily.com"
    tavily_api_key: str = ""

    extractor_timeout_seconds: int = 120

    source_cache_ttl_hours: int = 168
    source_max_chars: int = 50000

    # ======================================================
    # IMAGE
    # ======================================================
    image_provider: str = "openai"

    image_base_url: str = "https://api.openai.com/v1"
    image_api_key: str = ""
    image_model: str = "gpt-image-2"

    image_quality: str = "medium"
    image_max_count: int = 3
    image_timeout_seconds: int = 180

    # ======================================================
    # STRAPI
    # ======================================================
    strapi_base_url: str = "https://cms.example.com"
    strapi_api_token: str = ""

    strapi_blog_plural_api_id: str = "blogs"
    strapi_blog_uid: str = "api::blog.blog"

    #: Plural API ids of the author / category collections (M-3): the
    #: ``GET /api/{pluralApiId}`` endpoints are collection-type-specific,
    #: so both are configurable to match a given Strapi deployment.
    strapi_author_plural_api_id: str = "authors"
    strapi_category_plural_api_id: str = "categories"

    strapi_default_author_document_id: str = ""
    strapi_default_category_document_id: str = ""

    strapi_author_required: bool = True
    strapi_category_required: bool = True

    strapi_set_posted_at_on_draft: bool = False
    strapi_frontend_renders_main_image: bool = True
    strapi_timeout_seconds: int = 60

    # P8: optional admin-panel URL for the "Open in Strapi" link
    # (spec section 43.5). Defaults to ``<strapi_base_url>/admin``.
    strapi_admin_url: str = ""

    # ======================================================
    # ARTICLE
    # ======================================================
    article_default_language: str = "en"
    article_default_market: str = "US"
    article_min_image_count: int = 1
    article_max_image_count: int = 3

    seo_guideline_path: str = "prompts/seo_article_guideline.md"
    brand_visual_guideline_path: str = "prompts/brand_visual_guideline.md"

    # P9-A: anti-copy tuning (spec section 29 suggested thresholds).
    anti_copy_min_overlap_words: int = 12
    anti_copy_min_similarity: float = 0.85

    # ======================================================
    # P9-C: operations (Cleanup + Backup)
    # ======================================================
    # Retention window (days) for terminal jobs (failed/cancelled) before
    # the cleanup tool may delete them. 0 = keep forever (cleanup is a no-op
    # for jobs). Success jobs are NEVER auto-deleted regardless of this value.
    job_retention_days: int = 30
    # How many backup archives to keep (oldest beyond this are pruned).
    backup_keep: int = 14
    # Directory for backup archives (under data by default).
    backup_dir: str = ""

    # ======================================================
    # Derived helpers
    # ======================================================
    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url)

    @property
    def dataforseo_configured(self) -> bool:
        return bool(self.dataforseo_login and self.dataforseo_password)

    @property
    def exa_configured(self) -> bool:
        return bool(self.exa_api_key)

    @property
    def image_configured(self) -> bool:
        return bool(self.image_api_key)

    @property
    def strapi_configured(self) -> bool:
        return bool(self.strapi_api_token)

    @property
    def strapi_admin_panel_url(self) -> str:
        """URL of the Strapi admin panel ("Open in Strapi" link, P8).

        Falls back to ``<strapi_base_url>/admin`` when ``strapi_admin_url``
        is not set.
        """
        if self.strapi_admin_url:
            return self.strapi_admin_url.rstrip("/")
        return self.strapi_base_url.rstrip("/") + "/admin"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()
