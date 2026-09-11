# TASK: LLM 模型按场景区分（Model Tiering）

> **状态：已完成（2026-07），含端点拆分 follow-up（同日）。** 全量回归
> `python3 -m pytest` = **627 passed, 1 skipped**
> （唯一 skip 为需真实 Strapi 的 `test_strapi_pipeline.py`，与本 task 无关；端点拆分前
> 基线 `614 passed, 1 skipped` 不降，本次净增 13 个测试）。无 DB 迁移
> （`llm_usage.model` 列复用）。
> 新增/更新测试：`test_config.py`（2 更新 + 1 新增）、`test_llm_provider.py`（+4，tiering 期）、
> `test_p9b1_cost.py`（+1，tiering 期）、`test_p9_pipeline.py`（tiering 期 +1 + 本次 +1
> 双端点全链路 + 1 处成本测试 settings 对齐更新）、`tests/unit/test_llm_tiering.py`
> （本次 +11）、`test_p8_routes.py`（provider status 形状断言更新，tiering 期）。

> 立项依据：用户决策（2026-07）——写作与分析场景必须区分模型。
> 规范依据：`SEO-AUTO-DEV-SPEC.md` §10.1（LLMProvider 接口，冻结）、§48（llm_usage 台账）、§57（/settings 页）、§61（开发指令）。
> 前置关系：无阻塞前置；建议在 live 运行验收（真实 provider 全链路）之后或之前均可独立执行。

## 目标

将单一 `LLM_MODEL` 拆分为两档，按 pipeline 场景路由：

| 档 | 场景（15 步中的 LLM 调用点） | 默认温度 | 预期模型 |
|---|---|---|---|
| **写作档** (writing) | `article_writer`、`article_reviser`（2 次/job） | `LLM_TEMPERATURE_WRITING` / `LLM_TEMPERATURE_REVISION` | 最强可用模型 |
| **分析档** (analysis) | `competitor_analysis` ×5、`serp_synthesis`、`evidence_research`、`content_brief`、`outline`(+repair)、`seo_review`、`fact_review`、`style_review`、`image_plan`（13 次/job） | `LLM_TEMPERATURE_ANALYSIS` / `_REVIEW` / `_IMAGE_PLANNING` | 便宜快速的模型 |

合并三个 review 成一次调用**不在本 task 范围**（结论见下方"关联决策"），仅作为观察项跟踪。

## 设计原则

1. **不破坏冻结接口语义**：`LLMProvider.generate_structured` 增加**可选**参数
   `model: str | None = None`——`None` 时退回 provider 构建时的默认模型
   （即现有 `LLM_MODEL`），老调用点零改动、老测试零改动。§10.1 属冻结接口，
   本 task 视为"用户明确要求的接口扩展"，在本文档记录变更理由，不改规范原文。
2. **所有模型名/档位映射来自 Settings/.env**（规范 §61 第 5 条），业务代码零硬编码。
3. 每档独立可关闭：分析档模型留空时两档共用 `LLM_MODEL`，行为退回现状。

## 实现

### 1. Settings（`app/core/config.py`）

```
llm_model: str = "qwen3.8-27b"            # 不变，成为"写作档默认 + 全局兜底"
llm_model_analysis: str = ""              # 空 = 分析档也用 llm_model
```

`.env` / `.env.example` 同步，注释说明留空语义。

### 2. Provider（`app/providers/llm/base.py` + `openai_compatible.py`）

- `LLMProvider.generate_structured(..., model: str | None = None)`：抽象方法签名追加可选参数。
- `OpenAICompatibleLLMProvider`：实现内 `model or self._default_model`；请求日志与
  `llm_usage.model` 记录**实际生效的模型名**（台账口径不变，规范 §48）。
- `health_check` 只探默认模型（不变）。

### 3. 路由（`app/pipeline/steps/*`，15 个调用点中的 13 个）

分析档的 `generate_structured` 调用点追加显式分析档模型名（端点拆分
follow-up 后为 `... or get_settings().llm_model`，使独立端点在模型名留空
时也生效，见下方 Follow-up）：

```
competitor_analysis.py    (1 处, 循环内)
serp_synthesis.py         (1)
evidence_research.py      (1)
content_brief.py          (1)
outline.py                (2: 生成 + repair)
reviewers.py              (3: seo / fact / style)
image_plan.py             (1)
```

写作档 2 处（`article_writer.py`、`article_reviser.py`）**不加参数**，走默认 `LLM_MODEL`。

（以上 7 个文件共 10 个代码调用点，常规一次全跑合计 13 次分析调用
（competitor_analysis ×5 + 其余 8 个单调用点）+ 写作档 2 次 = **15 次/job**，
与 `test_cost_tracking_records_usage_and_provider_costs` 断言的 15 一致；
outline repair 分支（`MAX_OUTLINE_REPAIRS=2`）最坏再加 2 次 → 分析 15 / 总计 17。）

### 4. 可观测性

- `/settings` 页与 `GET /api/providers/status`（§57）：LLM 卡片增加
  `writing model` / `analysis model` 两行实际生效值（脱敏规则不变，模型名非 secret）。
- 日志：`pipeline_step_start` 已有 step 名；`llm_usage` 行已带 `model` 列，
  无需新列（迁移不涉及）。

### 5. 主要文件

```
app/core/config.py
app/providers/llm/base.py
app/providers/llm/openai_compatible.py
app/pipeline/steps/{competitor_analysis,serp_synthesis,evidence_research,
                    content_brief,outline,reviewers,image_plan}.py
app/routes/providers.py  (+ 静态页 providers 卡片)
.env.example
tests/unit/...
```

### 6. 测试（§59：unit 必做，integration 用 fake LLM）

- unit：
  - `test_llm_provider.py` 增：`model=None` 用默认模型；`model="x"` 请求体带 "x"；
    分析档留空时退回默认（向后兼容）。
  - `test_config.py` 增：`llm_model_analysis` 默认空 + .env 覆盖。
- integration（fake LLM 记录收到的 model 参数）：
  - 跑全 pipeline，断言 writer/reviser 收到默认模型、13 个分析调用收到
    `llm_model_analysis`；`llm_usage` 行 model 列与之一致。
- 全量回归：`python3 -m pytest`，不得跌破 `556 passed, 48 skipped` 基线。

## 验收

1. `LLM_MODEL=m-strong LLM_MODEL_ANALYSIS=m-cheap` 下真实跑 1 job：
   `llm_usage` 中 writer/reviser 行 model=m-strong，其余 13 行（分析档）m-cheap。
2. `LLM_MODEL_ANALYSIS` 留空：行为与改动前完全一致（15 次常规 / 17 次最坏 全走 m-strong）。
3. `/settings` 页正确显示两档实际生效模型。
4. 全量 pytest 不降基线。

## 关联决策（记录，不在本 task 执行）

- **三 review 合并**：不合并。理由：fact review 是安全门（§26.2 高危词逐条核查），
  合并摊薄注意力有质量风险；且涉及 §9/§26/§27/§46.14/§63 + 状态机 + UI 的
  规范级变更。改为观察项：live 验收后查 `llm_usage`，若 review 输入 token 占比
  显著，再单独立项评估。
- 成本对比预期：分析档 13 次/job（常规）× 便宜模型，是主要节省点；
  provider 层 repair/网络重试（§49、`LLM_MAX_RETRIES`）不受档位影响，照常工作。

## Follow-up：分析档独立端点（LLM_BASE_URL_ANALYSIS / LLM_API_KEY_ANALYSIS）

> 立项依据：用户决策（2026-07）——"两种模型都需要能单独配置
> LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL，这样可能写作用 Gemini，分析用
> OpenAI"。分析档在 `LLM_MODEL_ANALYSIS` 之上新增独立的 base_url / api_key，
> 三个字段**各自独立**留空即退回写作档对应值。

### 契约

- 路由：`generate_structured` 的 `model=None` → 写作档；显式模型名 → 分析档。
  10 个分析档调用点因此改传**有效分析档模型名**
  `get_settings().llm_analysis_model or get_settings().llm_model`——
  `LLM_MODEL_ANALYSIS` 留空时 = `LLM_MODEL`，调用仍是"显式分析档"，
  使独立端点在没有独立模型名时也能生效（单端点部署行为与拆分前逐字节一致）。
- `TieredLLMProvider`（`app/providers/llm/tiering.py`）持有至多 2 个
  `OpenAICompatibleLLMProvider`：分析档三元组（base_url/api_key/model 逐字段
  回退后）与写作档**完全相同**时只建 1 个 provider（零额外连接/开销）；
  任一字段不同才建第二个。分析档实例用 `settings.model_copy(update=...)`
  构造，其 `_settings.llm_model` 默认即分析档模型名。
- 溯源：`_last_model` / `begin_usage` / `take_usage` / `health_check` 全部
  按"最近一次实际服务的实例"语义代理（`begin_usage` 重置两个实例），
  meter 与 `llm_model_name` 无需任何改动，`llm_usage.model` 记录的始终是该次
  调用**实际发出**的模型名。
- `generate_text`（冻结接口无 model 参数）恒走写作档；pipeline 分析路径
  只用 `generate_structured`，无影响。
- `health_check` 覆盖所有已配置的档端点；`aclose` 只关自建 client。
- `build_providers`（`app/workers/article_tasks.py`）唯一真实构建点改用
  `TieredLLMProvider`；`/api/providers/status`（§57/§60）行为不变：可达性 =
  所有已配置 LLM 端点均可达，UI 只展示模型名（不展示 URL/key，§60）。

### 环境变量（`.env` / `.env.example`）

```
LLM_BASE_URL_ANALYSIS=    # 留空 = 分析档退回 LLM_BASE_URL
LLM_API_KEY_ANALYSIS=     # 留空 = 分析档退回 LLM_API_KEY
LLM_MODEL_ANALYSIS=       # 留空 = 分析档退回 LLM_MODEL（原语义）
```

### 验收

1. 双端点：写作用 A 端点（key_A/model_A）、分析用 B 端点（key_B/model_B），
   全链路 15 次调用按档落到对应端点；`llm_usage` 15 行 model 列与实际
   发出值逐行一致（`test_p9_pipeline.py::test_endpoint_split_two_endpoints_full_run`，
   线级断言 URL + Authorization + 请求体 model）。
2. 单字段拆分：只设 `LLM_API_KEY_ANALYSIS`（同 base_url 不同账号）也能生效
   （`test_llm_tiering.py::test_per_field_fallback_for_analysis_endpoint`）。
3. 全部留空：只建 1 个 provider，行为与端点拆分前一致
   （`test_llm_tiering.py::test_single_endpoint_when_analysis_unset` +
   既有 tiering 集成测试全绿）。
