"""Chinese error explanations for the job-detail error panel (P9-B3).

Maps every stable error code (spec section 52 ``ErrorCode`` — plus the
provider-derived / runtime codes that can reach ``generation_jobs.error_code``)
to:

- ``category``  — one-line Chinese cause (错误原因), e.g. ``LLM 不可用``;
- ``reason``    — a longer Chinese sentence explaining *why* it happened;
- ``advice``    — a Chinese disposition hint (处置建议): what to check or fix
  before retrying, and whether the step can be retried in place.

Unknown codes (e.g. provider-derived ``HTTP_429`` / ``HTTP_500`` or the
``UNEXPECTED`` catch-all) fall back to :func:`generic_explanation` so the
panel always renders something useful instead of breaking.

The catalog is deliberately UI-facing: the *machine-readable* contract
(``error_code`` / ``error_message`` in the DB and the REST ``error`` block)
is untouched — this module only adds the human layer on top.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.exceptions import ErrorCode


@dataclass(frozen=True)
class ErrorExplanation:
    """The three human-readable lines the error panel shows."""

    category: str
    reason: str
    advice: str


#: error_code -> (category, reason, advice). Keys mirror the stable codes in
#: ``ErrorCode`` (section 52) plus the derived codes written by the
#: providers / orchestrator catch-all / source-extract NO_PAGE branch.
_EXPLANATIONS: dict[str, tuple[str, str, str]] = {
    # ---- DataForSEO (step 2) ----
    ErrorCode.DATAFORSEO_AUTH_FAILED.value: (
        "DataForSEO 认证失败",
        "调用 DataForSEO SERP API 时密钥被拒绝，通常是 DATAFORSEO 的 Key 或 "
        "Password 配置错误或已失效。",
        "检查 .env 中 DATAFORSEO_KEY / DATAFORSEO_PASSWORD 是否正确、账户是否欠费，"
        "修正后可使用「Retry (Full)」重新执行。",
    ),
    ErrorCode.DATAFORSEO_REQUEST_FAILED.value: (
        "DataForSEO 请求失败",
        "DataForSEO 返回了非成功状态或请求超时/网络异常，可能是接口限流或临时故障。",
        "稍等片刻后重试；持续失败时检查账户额度（429 为限流）与网络。",
    ),
    ErrorCode.DATAFORSEO_EMPTY_SERP.value: (
        "SERP 结果为空",
        "DataForSEO 调用成功但该关键词/市场没有任何 SERP 结果。",
        "换一个更具体或更符合目标市场（language/market）的关键词；原样重试不会改变结果。",
    ),
    # ---- Extractor (step 3) ----
    ErrorCode.EXTRACTOR_AUTH_FAILED.value: (
        "正文提取服务认证失败",
        "Exa 正文提取的 API Key 无效或已过期。",
        "检查 EXA_API_KEY 配置后重新执行「Source extraction」一步（从该步重试）。",
    ),
    ErrorCode.EXTRACTOR_FAILED.value: (
        "正文提取失败",
        "Exa 提取接口调用失败（未配置、限流或网络异常）。",
        "检查 Exa 配置/额度后从该步重试；配置 TAVILY_API_KEY 可启用 Tavily 兜底提取。",
    ),
    ErrorCode.SOURCE_EMPTY.value: (
        "无法提取有效正文",
        "所有候选页面的正文都过短或为空，无法进入竞品分析。",
        "勾选「Force Refresh Sources」后重试（重新抓取页面）；仍失败时更换关键词。",
    ),
    # ---- LLM (steps 4-14) ----
    ErrorCode.LLM_UNAVAILABLE.value: (
        "LLM 不可用",
        "未配置 LLM（OPENAI_BASE_URL / API Key）或模型服务无法访问。",
        "检查 LLM 配置与网络后重试；若失败步骤之后的产物不受影响，可用「从该步重试」只重跑失败步骤。",
    ),
    ErrorCode.LLM_STRUCTURED_OUTPUT_INVALID.value: (
        "LLM 结构化输出校验失败",
        "模型返回的内容无法通过结构化解析/校验（JSON 不完整或字段缺失）。",
        "原样重试通常可以恢复（模型输出有随机性）；反复失败时调整 prompt 或更换更强的模型。",
    ),
    ErrorCode.LLM_CONTEXT_OVERFLOW.value: (
        "LLM 上下文超限",
        "输入内容超过模型上下文窗口（来源正文过多或过长）。",
        "减少来源数量或精简来源内容后重试；必要时更换更长上下文的模型。",
    ),
    # ---- Article (steps 8-9) ----
    ErrorCode.ARTICLE_VALIDATION_FAILED.value: (
        "文章校验失败",
        "文章/大纲等产物未通过结构校验（例如 Strapi 推送前缺少必需内容）。",
        "查看该步骤产物后使用「从该步重试」重新生成。",
    ),
    # ---- Image (steps 14-15) ----
    ErrorCode.IMAGE_PROVIDER_FAILED.value: (
        "图片生成失败",
        "图片服务（provider）调用失败：未配置、鉴权失败、限流或内容审查拒绝。",
        "检查图片服务配置/额度后从「Image generation」步骤重试；该步可部分成功，重试会补齐缺失图片。",
    ),
    ErrorCode.IMAGE_PLAN_INVALID.value: (
        "图片规划无效",
        "图片规划结果未通过校验（计划数量/角色/alt 文本不合规）。",
        "原样重试通常可以恢复；反复失败时检查图片规划 prompt 或更换模型。",
    ),
    # ---- Strapi (sync phase) ----
    ErrorCode.STRAPI_AUTH_FAILED.value: (
        "Strapi 认证失败",
        "Strapi 草稿推送使用的 Token 无效或已过期。",
        "更新 STRAPI_API_TOKEN 后再次执行「Push Draft to Strapi」。",
    ),
    ErrorCode.STRAPI_SCHEMA_MISMATCH.value: (
        "Strapi 数据模型不匹配",
        "推送到 Strapi 的字段与 CMS 数据模型定义不一致。",
        "核对 Strapi 端内容类型/组件字段定义后重试。",
    ),
    ErrorCode.STRAPI_SLUG_CONFLICT.value: (
        "Strapi Slug 冲突",
        "目标 slug 已存在于 Strapi，为避免覆盖已有文章而未创建草稿。",
        "在 Strapi 中处理冲突 slug，或修改文章标题/关键词使 slug 唯一后重试推送。",
    ),
    ErrorCode.STRAPI_UPLOAD_FAILED.value: (
        "Strapi 媒体上传失败",
        "图片上传到 Strapi 失败（网络或 CMS 侧错误）。",
        "稍后再次执行「Push Draft to Strapi」；已成功的上传不会重复执行。",
    ),
    ErrorCode.STRAPI_DRAFT_CREATE_FAILED.value: (
        "Strapi 草稿创建失败",
        "调用 Strapi 创建草稿接口失败（接口不可用或请求被拒）。",
        "检查 Strapi 服务与 Token 后重试推送。",
    ),
    ErrorCode.STRAPI_DRAFT_UPDATE_FAILED.value: (
        "Strapi 草稿更新失败",
        "更新已有 Strapi 草稿失败（草稿已被删除或权限不足）。",
        "在 Strapi 中确认草稿存在后重试，或使用「Push Draft to Strapi」重新创建。",
    ),
}


def generic_explanation(code: str | None, message: str | None) -> ErrorExplanation:
    """Fallback for codes without a dedicated entry (HTTP_*, UNEXPECTED, …)."""
    if code and code.startswith("HTTP_"):
        return ErrorExplanation(
            f"上游接口返回 {code[5:]} 错误",
            f"外部服务返回 HTTP {code[5:]} 状态码：{message or '无附加信息'}",
            "稍后重试；若持续出现，检查对应服务的额度/配额与网络连通性。",
        )
    if code == "NO_PAGE":
        return ErrorExplanation(
            "页面无法抓取",
            f"部分来源页面抓取失败：{message or '无附加信息'}",
            "属部分成功场景；可勾选「Force Refresh Sources」重试抓取，或直接继续后续步骤。",
        )
    return ErrorExplanation(
        "未知错误",
        message or "未记录的错误（UNEXPECTED）",
        "查看该 Job 的 Logs 部分定位异常；如确认为临时性故障，可「从该步重试」。",
    )


def explain_error(
    code: str | None,
    message: str | None,
    *,
    last_failed_step: int | None = None,
    step_label: str | None = None,
) -> dict:
    """Build the error-panel payload for one failed job.

    ``last_failed_step`` (1..15) is the step whose checkpoint was never
    committed; when present the panel offers a one-click retry of exactly
    that step (P9-A ``retry_step``). ``step_label`` is the human label of
    that step (``checkpoints.STEP_LABELS``).
    """
    key = (code or "").strip() or None
    if key in _EXPLANATIONS:
        category, reason, advice = _EXPLANATIONS[key]
    else:
        fallback = generic_explanation(key, message)
        category, reason, advice = (
            fallback.category,
            fallback.reason,
            fallback.advice,
        )
    payload: dict = {
        "code": code,
        "message": message,
        "category": category,
        "reason": reason,
        "advice": advice,
        "last_failed_step": last_failed_step,
        "last_failed_step_label": step_label,
    }
    return payload
