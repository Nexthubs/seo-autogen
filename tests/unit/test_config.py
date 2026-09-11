"""Settings come from .env / environment (spec sections 11, 61.5)."""

from app.core.config import Settings


def test_settings_defaults():
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.app_port == 8080
    assert settings.llm_provider == "openai_compatible"
    assert settings.llm_model == "qwen3.8-27b"
    assert settings.serp_provider == "dataforseo"
    assert settings.dataforseo_request_type == "standard"
    assert settings.dataforseo_os == "windows"
    assert settings.image_max_count == 3
    assert settings.article_min_image_count == 1
    assert settings.strapi_frontend_renders_main_image is True
    assert settings.source_cache_ttl_hours == 168
    # Model tiering (TASK-LLM-MODEL-TIERING): analysis tier defaults to
    # empty, which routes back to the default model (pre-tiering behaviour).
    assert settings.llm_model_analysis == ""
    assert settings.llm_analysis_model is None
    # Endpoint split (TASK-LLM-MODEL-TIERING follow-up): analysis endpoint
    # fields default to empty -> every analysis value falls back to the
    # writing tier, and no distinct second endpoint is configured.
    assert settings.llm_base_url_analysis == ""
    assert settings.llm_api_key_analysis == ""
    assert settings.llm_analysis_base_url == settings.llm_base_url
    assert settings.llm_analysis_api_key == settings.llm_api_key
    assert settings.llm_analysis_endpoint_distinct is False


def test_llm_model_analysis_tiering(monkeypatch):
    """Analysis tier: empty -> None (fallback); set -> the override name."""
    # Empty (default): analysis tier falls back to the default model.
    base = Settings(_env_file=None)
    assert base.llm_model == "qwen3.8-27b"
    assert base.llm_analysis_model is None

    # Set: analysis tier uses the dedicated cheaper model.
    monkeypatch.setenv("LLM_MODEL_ANALYSIS", "qwen3.8-8b")
    settings = Settings(_env_file=None)
    assert settings.llm_model == "qwen3.8-27b"
    assert settings.llm_model_analysis == "qwen3.8-8b"
    assert settings.llm_analysis_model == "qwen3.8-8b"
    # A distinct model name alone makes the analysis tier "distinct".
    assert settings.llm_analysis_endpoint_distinct is True


def test_llm_analysis_endpoint_fallback(monkeypatch):
    """Each LLM_*_ANALYSIS field falls back to the writing-tier value
    when empty; a distinct second connection is configured only when at
    least one field genuinely differs (endpoint split follow-up)."""
    monkeypatch.setenv("LLM_BASE_URL", "http://writer.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "writer-key")
    monkeypatch.setenv("LLM_MODEL", "writer-model")

    # All empty -> analysis tier is exactly the writing tier (no distinct).
    s = Settings(_env_file=None)
    assert s.llm_analysis_base_url == "http://writer.test/v1"
    assert s.llm_analysis_api_key == "writer-key"
    # Empty model: None by contract (the caller passes the fallback).
    assert s.llm_analysis_model is None
    assert s.llm_analysis_endpoint_distinct is False

    # Only a distinct base URL -> distinct endpoint (second connection).
    monkeypatch.setenv("LLM_BASE_URL_ANALYSIS", "http://analysis.test/v1")
    s = Settings(_env_file=None)
    assert s.llm_analysis_base_url == "http://analysis.test/v1"
    assert s.llm_analysis_api_key == "writer-key"  # key still falls back
    assert s.llm_analysis_model is None  # empty -> None by contract
    assert s.llm_analysis_endpoint_distinct is True

    # Distinct API key alone also counts (same vendor, different account).
    monkeypatch.delenv("LLM_BASE_URL_ANALYSIS")
    monkeypatch.setenv("LLM_API_KEY_ANALYSIS", "analysis-key")
    s = Settings(_env_file=None)
    assert s.llm_analysis_base_url == "http://writer.test/v1"
    assert s.llm_analysis_api_key == "analysis-key"
    assert s.llm_analysis_endpoint_distinct is True

    # Base URL set to the SAME value as writing tier -> not distinct.
    monkeypatch.delenv("LLM_API_KEY_ANALYSIS")
    monkeypatch.setenv("LLM_BASE_URL_ANALYSIS", "http://writer.test/v1")
    s = Settings(_env_file=None)
    assert s.llm_analysis_endpoint_distinct is False

    # Trailing slash differences are normalised away.
    monkeypatch.setenv("LLM_BASE_URL_ANALYSIS", "http://writer.test/v1/")
    s = Settings(_env_file=None)
    assert s.llm_analysis_endpoint_distinct is False


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3.8-27b-instruct")
    monkeypatch.setenv("DATAFORSEO_LOGIN", "user@example.com")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "pw")
    monkeypatch.setenv("DATAFORSEO_REQUEST_TYPE", "live")
    monkeypatch.setenv("DATAFORSEO_DEVICE", "mobile")
    monkeypatch.setenv("DATAFORSEO_OS", "ios")
    monkeypatch.setenv("IMAGE_MAX_COUNT", "2")

    settings = Settings(_env_file=None)
    assert settings.llm_base_url == "http://127.0.0.1:1234/v1"
    assert settings.llm_model == "qwen3.8-27b-instruct"
    assert settings.image_max_count == 2
    assert settings.dataforseo_configured is True
    assert settings.dataforseo_request_type == "live"
    assert settings.dataforseo_device == "mobile"
    assert settings.dataforseo_os == "ios"


def test_configured_properties():
    settings = Settings(_env_file=None)
    # Defaults: no secrets configured.
    assert settings.dataforseo_configured is False
    assert settings.exa_configured is False
    assert settings.image_configured is False
    assert settings.strapi_configured is False
    assert settings.llm_configured is True
