# 审计修复进度跟踪（CODEX Audit → 分批修复）

> 依据：`CODEX-AUDIT-REPORT.md`（审计 commit `c5c7101`，结论：整体验收不通过；
> 0 Critical / 12 High / 15 Medium / 3 Low，共 30 项）。
>
> 修复顺序遵循 AGENTS.md 的 P0 → P9 阶段约束与报告 §11 建议，
> 每批次完成后更新本文档并运行完整测试套件（`unit + integration`，PostgreSQL 本地实例）。
> 内容规则以 `SEO-AUTO-DEV-SPEC.md` 为最高优先级；不调用真实付费 API / 不写真实 Strapi，
> 全部使用 mock / fixture 验证。

## 基线

| 项 | 值 |
|---|---|
| 审计 commit | `c5c71010e91927de0507be490de7cd6a80da34ab` |
| 基线测试 | `415 passed, 1 skipped`（本地 PostgreSQL 127.0.0.1:5434 可用） |
| 测试命令 | `PYTHONDONTWRITEBYTECODE=1 RUN_EXTERNAL_INTEGRATION_TESTS=false .venv/bin/python -m pytest -q -ra -p no:cacheprovider` |
| 修复批次划分 | B1(P0/P1) → B2(P2) → B3(P3) → B4(P4/P5) → B5(P6) → B6(P7) → B7(P8/P9+文档) |

## 批次总览

| 批次 | 阶段 | 修复项 | 状态 |
|---|---|---|---|
| B1 | P0 / P1 | M04, H08, L02, M09, M11 | ✅ 完成（见下） |
| B2 | P2 | H01, H02, M15 | ⏳ 待办 |
| B3 | P3 | H04, M05 | ⏳ 待办 |
| B4 | P4 / P5 | H09, H06, H07, H11, M08 | ⏳ 待办 |
| B5 | P6 | H05, H10, M06, M07, L01 | ⏳ 待办 |
| B6 | P7 | H03, M01, M02 | ⏳ 待办 |
| B7 | P8 / P9 + 文档 | M13, M14, M12, M10, M03, H12, L03 | ⏳ 待办 |

> 决策记录：H11（不可变文章历史，属 P5/P9）原可放 B1 的 P1 范围，
> 但修复依赖 P4/P5 的 checkpoint/版本语义重构，故移到 B4（P4/P5 批次）一并处理。

## 明细跟踪表

状态图例：✅ 已修复（含测试证据）｜🔧 进行中｜⏳ 待办

### B1 — P0 / P1 ✅

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| M04 | Med | Dockerfile 漏拷贝 `alembic.ini`，容器内无法按 README 执行迁移 | L14 COPY 列表加入 `pyproject.toml README.md alembic.ini ./` | `Dockerfile` | 静态核对（镜像构建环境无 Docker） | ✅ |
| H08 | High | RQ 默认 180s 超时会提前杀死整条长 pipeline | 新增可配置 `rq_job_timeout_seconds: int = 3600`；`enqueue_job` / `enqueue_strapi_sync` 显式传 `timeout=settings.rq_job_timeout_seconds`；`.env.example` 同步说明 | `app/core/config.py`、`app/workers/article_tasks.py`、`.env.example` | `tests/unit/test_h08_rq_timeout.py`（4 用例：enqueue 传 timeout、options 路径、strapi_sync、默认值 3600） | ✅ |
| L02 | Low | schema 重复声明 `provider_cost` | 删除 `serp.py`、`sources.py` 以及扫描发现 `images.py` 中的重复字段 | `app/schemas/serp.py`、`app/schemas/sources.py`、`app/schemas/images.py` | 全仓重复字段扫描（`app/**` 无重复）；既有 cost 测试全绿 | ✅ |
| M09 | Med | usage 行 model 恒为 None；`llm_usage` 无 prompt 溯源；repair 后 outline 仍标 generator hash | ① `MeteredLLMProvider` 接收并持有 `settings`（orchestrator 传入），`llm_model_name` 正常取到 model；② `llm_usage` 增加 `prompt_name/prompt_version/prompt_hash` 列（迁移 0011）；③ 新增 `set_llm_prompt()` helper，9 个步骤文件的 13 处 LLM 调用前设置 `current_prompt`；④ outline 修复轮记录 repair prompt 溯源，持久化行按最终产出 prompt（`repair_count>0` 用 repair） | `app/services/llm_metering.py`、`app/pipeline/steps/_common.py`、`app/db/models/llm_usage.py`、`app/pipeline/orchestrator.py`、`app/pipeline/steps/{competitor_analysis,serp_synthesis,evidence_research,content_brief,outline,article_writer,article_reviser,reviewers,image_plan}.py`、`migrations/versions/20260917_0011_error_raw_usage_prompt.py` | `tests/unit/test_p9b1_cost.py`：`test_records_prompt_provenance_when_set`、`test_prompt_columns_null_when_unset`、`TestSetLLMPrompt`（2 用例） | ✅ |
| M11 | Med | 失败 raw（模型原文/traceback）只留在异常对象，重启后不可查 | ① `generation_jobs.error_raw` 新列（迁移 0011）；② orchestrator `PipelineError` 分支写 `error.raw`、UNEXPECTED 分支写完整 traceback；③ 已核对 provider：结构化输出失败已带 `raw=last_output`（spec §49 满足，无需改）；④ `GET /api/jobs/{id}` 的 error payload 暴露 `error_raw`；⑤ retry 时清空 `error_raw` | `app/db/models/job.py`、`app/pipeline/orchestrator.py`、`app/routes/jobs.py`、`migrations/versions/20260917_0011_error_raw_usage_prompt.py` | `tests/unit/test_p9_reliability.py::TestErrorRawLifecycle`（3 用例：暴露/缺失为 null/retry 清空）；`tests/integration/test_generation_job.py` 表结构断言加入 `error_raw`；`tests/unit/test_p9b3_error_ui.py` 断言新键 | ✅ |

**B1 测试结果**：`426 passed, 1 skipped`（基线 415 + 新增 11），迁移 `0011_error_raw_usage_prompt` 已升级至本地 PG 测试库。

### B2 — P2（⏳）

| ID | 级别 | 问题摘要 | 计划 | 状态 |
|---|---|---|---|---|
| H01 | High | DataForSEO 完整响应契约错误（`20000` / `tasks[].result[].items[]` / `rank_group`） | 按官方 Live Advanced 契约重写解析；fixtures 用官方形状；覆盖 task 失败 / PAA / 混合 / raw 保留 | ⏳ |
| H02 | High | Exa 未显式请求 `text`，依赖未声明的服务默认值 | 显式 `text=true`（必要时 markdown），测试约束请求参数与响应 | ⏳ |
| M15 | Med | health 可能付费（调 live SERP）且 HTTP200 业务鉴权失败判 Connected；Tavily 401/403 也算正常 | 低成本认证探测 + 严格业务成功校验；false-positive 用例 | ⏳ |

### B3 — P3（⏳）

| ID | 级别 | 问题摘要 | 计划 | 状态 |
|---|---|---|---|---|
| H04 | High | 同步 `prepare_keyword` 返回 `KeywordMetrics` 被 orchestrator lambda 当非 awaitable，已知词必然 TypeError | 步骤改 async（或统一 adapter 合同）；已知词 / 未知词完整 orchestrator 用例 | ⏳ |
| M05 | Med | `autoflush=False` 下重复新词导入 IntegrityError；`ilike` 未转义 `%`/`_` | 导入前去重 + flush / DB upsert；case-fold 精确比较；生产同款 session 测试 | ⏳ |

### B4 — P4 / P5（⏳）

| ID | 级别 | 问题摘要 | 计划 | 状态 |
|---|---|---|---|---|
| H09 | High | evidence 来源全部由同一 LLM 自述，无真实性核查 | 独立可审计的来源核实（Exa/搜索存在性检查）；无证据时降级为谨慎非事实陈述 | ⏳ |
| H06 | High | DoD gate 形同虚设（FAQ/CTA 只查标题、无 Setext H1、图片不查文件、畸形 marker 放过） | Markdown 解析结构检查 + 关联研究/审核/图片完整性；负向 fixture | ⏳ |
| H07 | High | 最终导出与 Strapi 同步未解析内链 marker | 共享 final renderer：先内链后图片；断言无残留 marker | ⏳ |
| H11 | High | retry 删除全部 article_versions，文章历史不可追溯 | 区分当前 checkpoint 与不可变历史（run/attempt/current-version 关联）；重试后旧版本保留、新版本递增 | ⏳ |
| M08 | Med | `job.strategy` 未进入 Brief prompt；writing/review 温度硬编码，`.env` 调参无效 | 贯通 strategy 与 Settings 温度到各步骤；unsupported 输入限制/说明 | ⏳ |

### B5 — P6（⏳）

| ID | 级别 | 问题摘要 | 计划 | 状态 |
|---|---|---|---|---|
| H05 | High | image step 提前 commit `ready`，DoD gate 在其后；sync 入口无状态门禁 | 仅 orchestrator 同事务 DoD 后设 ready/completed_at；sync 服务端核验状态 | ⏳ |
| H10 | High | 图片下载向任意响应 URL 带 Images API key | 下载不带服务凭证（或严格同源 + 重定向处理）；跨域/预签名/重定向测试 | ⏳ |
| M06 | Med | 图片重试全部重新生成已成功图片 | 复用已验证 local_path，仅重做失败项；regenerate vs resume 语义 | ⏳ |
| M07 | Med | `.webp` 文件可能是 PNG 字节 | 请求/转码 WebP；解码 + 文件头 + MIME 一致性测试 | ⏳ |
| L01 | Low | 本地 artifact 目录缺 spec §34 列出的研究 JSON | 补全 content-brief/outline/serp/review/sources 导出 | ⏳ |

### B6 — P7（⏳）

| ID | 级别 | 问题摘要 | 计划 | 状态 |
|---|---|---|---|---|
| H03 | High | Strapi 5 扁平 data / 数组上传 / relation populate 契约不兼容 | 按 Strapi 5 REST 契约重写解析；schema 修正 relation/media；官方形状 mock | ⏳ |
| M01 | Med | sync 失败后 job 停 `strapi_syncing`，UI 无错误区/重试入口 | 统一持久化失败状态（FAILED + sync_status，保留 documentId）；UI/API 可重试 | ⏳ |
| M02 | Med | GET 验证只查非空，不核对字段值与 Draft 状态 | 逐字段规范化比对 + documentId/媒体/relations + Draft 语义 | ⏳ |

### B7 — P8 / P9 + 文档（⏳）

| ID | 级别 | 问题摘要 | 计划 | 状态 |
|---|---|---|---|---|
| M13 | Med | Job Detail 无 competitor/synthesis/evidence 研究正文与 Logs 区 | 结构化只读呈现 + 按步骤错误日志 | ⏳ |
| M14 | Med | enqueue 失败被当正常 queued，UI 无恢复提示 | 显式排队失败状态 + 安全重投（outbox 语义）；Redis down→up 恢复测试 | ⏳ |
| M12 | Med | 成本随重试删除/覆盖、缓存归属错误（读 credits 而非 cost） | 不可变请求账本 + 当前 artifact 分离；cache hit 记零新增 | ⏳ |
| M10 | Med | JSON 序列化 secret 与异常消息脱敏不可靠 | 递归脱敏（JSON/URL 参数/Bearer/响应回显） | ⏳ |
| M03 | Med | Preview 输出原始 Markdown，缺 inline 图片 | 安全 Markdown 渲染 + 图片 placement；heading/table/link/alt/恶意 HTML 测试 | ⏳ |
| H12 | High | 备份含图片但 restore 不恢复图片 | restore 解包 artifacts.tar + 路径/摘要校验；全新 DATA_DIR 恢复验证 | ⏳ |
| L03 | Low | README/PHASE-LOG “全部实现完成”口径高于实际 | 修正文档：附 commit、环境、mock/live 范围、待修复列表 | ⏳ |

## 变更日志

- **2026-09-17 — B1 完成**：M04 / H08 / L02 / M09 / M11 全部修复并测试（`426 passed, 1 skipped`）；
  新增迁移 `0011_error_raw_usage_prompt`（`generation_jobs.error_raw` + `llm_usage` prompt 三列）；
  新增测试 `tests/unit/test_h08_rq_timeout.py`，扩展 `test_p9b1_cost.py` / `test_p9_reliability.py` /
  `test_p9b3_error_ui.py` / `test_generation_job.py`。H11 移入 B4。
