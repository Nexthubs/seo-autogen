"""Settings come from .env / environment (spec sections 11, 61.5)."""

from app.core.config import Settings


def test_settings_defaults():
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.app_port == 8080
    assert settings.llm_provider == "openai_compatible"
    assert settings.llm_model == "qwen3.8-27b"
    assert settings.serp_provider == "dataforseo"
    assert settings.image_max_count == 3
    assert settings.article_min_image_count == 1
    assert settings.strapi_frontend_renders_main_image is True
    assert settings.source_cache_ttl_hours == 168


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3.8-27b-instruct")
    monkeypatch.setenv("DATAFORSEO_LOGIN", "user@example.com")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "pw")
    monkeypatch.setenv("IMAGE_MAX_COUNT", "2")

    settings = Settings(_env_file=None)
    assert settings.llm_base_url == "http://127.0.0.1:1234/v1"
    assert settings.llm_model == "qwen3.8-27b-instruct"
    assert settings.image_max_count == 2
    assert settings.dataforseo_configured is True


def test_configured_properties():
    settings = Settings(_env_file=None)
    # Defaults: no secrets configured.
    assert settings.dataforseo_configured is False
    assert settings.exa_configured is False
    assert settings.image_configured is False
    assert settings.strapi_configured is False
    assert settings.llm_configured is True
