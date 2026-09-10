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
| B2 | P2 | H01, H02, M15 | ✅ 完成（见下） |
| B3 | P3 | H04, M05 | ✅ 完成（见下） |
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

### B2 — P2 ✅

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| H01 | High | DataForSEO 响应契约完全错误：旧解析把 `tasks[].result` 当单个 `{organic, questions, related[]}` dict，读 `position` 作 rank；官方实为 `result` = **block 列表**，`items[]` 为**扁平混合类型**（按 item 自身 `type` 分派），rank 取 `rank_group`（`position` 是 left/right 字符串），PAA 是 `people_also_ask` item 下 `items[]` 的 `people_also_ask_element`（问题=element `title`，来源 url=首个 `expanded_element.url`，可能缺省），related 是 item 内 `items[]` **字符串列表**；另缺 featured snippet | 按官方 Live Advanced 契约（对官方脱敏样例逐字段机验）重写 `parse()`：逐 block × 逐 item 按 `type` 分派 organic/paa/related/featured；`search()` 用 `rank_group` 排序、PAA 嵌套解析（无 url 时 `source_url=None` 防御）、related 取字符串、featured 取首个带 url 的 item 映射 `FeaturedSnippet`；`SerpResult.result_type='featured'` 落库（`VARCHAR(32)` 已允许，**无需迁移**）；`serp_search_done` 日志计入 featured；模块 docstring 记录完整官方契约 | `app/providers/serp/dataforseo.py`、`app/schemas/serp.py`（新增 `FeaturedSnippet` + `SERPResponse.featured_snippet`）、`app/pipeline/steps/serp_search.py` | `tests/unit/test_dataforseo_provider.py` **整文件重写**（官方信封 fixtures，23 用例：happy path / `rank_group` 乱序排序 / 混合类型只取 organic / featured 有与无 / PAA 无 url 防御 / 空 organic→`DATAFORSEO_EMPTY_SERP` / task 失败 `result:null`→`DATAFORSEO_REQUEST_FAILED` / HTTP200+业务失败→`REQUEST_FAILED` / retry 429·401·500×3 / cost 取 `cost` 非 `credits` / health 未配置·401·200+业务失败·成功）；`scripts/p2_acceptance.py` 官方形状 + featured 落库断言（**PASS**） | ✅ |
| H02 | High | Exa 请求体只发 `{"ids": [...]}`，`text` 依赖服务端默认（默认 `false` → 无正文 → 每页 SOURCE_EMPTY） | `extract()` 显式发 `{"ids": urls, "text": True}`（API 默认 `text=false`，不显式声明则拿不到正文）；`/contents` docstring 同步标注 `text` 必须显式；新增“结果全缺 `text` → SOURCE_EMPTY”用例 | `app/providers/extractor/exa.py` | `tests/unit/test_exa_extractor.py`：`test_extract_sends_api_key_and_ids` 断言 body == `{"ids": [...], "text": True}`；新增 `test_extract_results_without_text_raise_source_empty` | ✅ |
| M15 | Med | health_check 假阳性 + 成本不清：DataForSEO 只判“无传输错误”（HTTP200 业务鉴权失败 `status_code!=20000` 会判 Connected）；Exa 走未文档化的 `GET /status` 且 `status_code<500`（401/403/404 也算健康）；Tavily `status_code<500`（401/403 算健康） | **DataForSEO**：`health_check()` 真实（付费，~1 次 SERP，`depth=1`）请求后额外 `self.parse(raw)`，要求顶层**且**task 级 `status_code==20000` 且 `result` 非空，任何 `PipelineError`（401/403/业务失败/空 SERP）→ False。**Exa**：改为**零成本认证探测**——POST `/search` 带故意非法 body（缺 `query`），Exa 先校验 `x-api-key` 再处理 body，故坏 key→401/403→False（未计费），有效 key→400/422→True（key 被接受，body 被拒）；彻底消除 401/403 假阳性且不付费。**Tavily**：保留真实 `/extract` 付费探测（单次 1 次抽取，**成本已文档化**），但严格要求 HTTP 200 且 body 含 `results`(list) + `failed_results`(list)；401/403/其它非 200/畸形 body → False | `app/providers/serp/dataforseo.py`、`app/providers/extractor/exa.py`、`app/providers/extractor/tavily.py` | DataForSEO：`test_health_check_*` 4 用例（未配置 / HTTP401 / HTTP200+业务失败40101 / 成功）。Exa：`test_health_check_*` 5 用例（未配置 / 400·422 有效 key→True / 401 / 403 / 传输错误→False）。Tavily：`test_p9_reliability.py::TestTavilyExtractor` 新增 7 用例（未配置 / 401 / 403 / 200 畸形 / 200 results 非 list / 200 正确形状→True / 传输错误→False） | ✅ |

**B2 测试结果**：`448 passed, 1 skipped`（B1 后 426 + 新增 22：DataForSEO 重写 +9、Exa +5、Tavily +7、cost +1）。`scripts/p2_acceptance.py` 对本地 PG 实跑 **ACCEPTANCE: PASS**（8 organic / 3 PAA / 1 featured 落库、Top-5 唯一 URL、去重回填、二次命中缓存 0 次抽取）。**无需新迁移**（`serp_results.result_type` 为 `VARCHAR(32)` 且已含 "featured"）。

> **M15 成本权衡（已文档化）**：三家 provider 的“连通性”信号不同——
> DataForSEO **恒返回 HTTP 200**，真实结果只在 body `status_code`（20000=ok），故 health 必须发一次真实付费 SERP 调用来区分鉴权失败；
> Exa **无文档化免费 ping 端点**（`GET /status` 未在 docs 声明），采用零成本的“非法 body 认证探测”（key 校验先于 body 处理）；
> Tavily **无免费端点**，保留真实付费 `/extract` 探测但要求业务成功形状。三者都彻底消除“401/403 被判 Connected”的假阳性。
>
> **M12 前置子项（B2 顺手完成）**：`_extract_cost()` 改读官方 `cost`（USD，task 级优先、顶层兜底），不再读 `credits`——这是 B7 M12 的“读错字段”部分；M12 的“不可变请求账本 + cache-hit 零计”主体仍在 B7。

### B3 — P3 ✅

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| H04 | High | 同步 `prepare_keyword` 是 15 个步骤中唯一的 sync 步骤：orchestrator 统一 `await` 每个 runner，**已知词**返回非 None 的 `KeywordMetrics`（非 awaitable）→ `TypeError` → job 必然失败；**未知词**返回 `None` 侥幸通过，所以既有集成测试（直接同步调用 + SERP-only 全链路）从未暴露 | `prepare_keyword` 改为 `async def`（对齐其余 14 个步骤的 async 合同；body 不变）；`content_brief` 本就自查 `lookup_metrics`，返回值无其它消费者，改动安全；两处直接调用集成测试改 `async def` + `await` | `app/pipeline/steps/keyword_prepare.py`、`tests/integration/test_keyword_dataset.py`（2 处调用点） | **新增 orchestrator 级回归** `tests/integration/test_p9_pipeline.py::test_known_keyword_full_run_reaches_ready`：dataset 内关键词（`prepare_keyword` 返回非 None）跑完整 15 步 fake-provider 链，断言 `ready` + `keyword_metrics_available=True` + `error_code is None`（旧代码此路径必 `TypeError`/FAILED）；既有 SERP-only 全链路用例继续覆盖未知词路径 | ✅ |
| M05 | Med | ① 生产 session `autoflush=False`：同一 sheet 内仅大小写不同的重复**新词**（如 "SEO Tips"/"seo tips"）双双通过 DB 查重 → 双插入 → commit 触发 `UNIQUE(cluster_id, keyword)` → IntegrityError；② `lookup_metrics`/`import_workbook`/`query_dataset` 用 `ilike(裸串)`，关键词含 `%`/`_` 时被当通配符（`"100% seo"` 会命中 `"100 seo"`）→ 串号数据 | ① `import_workbook` 每 sheet 先按 `casefold` 内存去重（后行覆盖前行 + warning），再入库——与 flush 策略无关；② 全部关键词/簇名比较改为 `lower(col) == 字面量.lower()` 精确匹配（消除 LIKE 通配符语义，`%`/`_` 按字面量处理） | `app/services/keyword_import.py`、`app/services/keyword_service.py` | ① **生产同款 session 测试**：新增 `db_noautoflush` fixture（`sessionmaker(autoflush=False)`，与 `SessionLocal` 同策略）+ `test_duplicate_new_word_same_sheet_no_integrity_error`（旧代码必 IntegrityError）+ `test_duplicate_new_word_case_insensitive_rerun_upserts`（`%` 关键词重导入 upsert 不串号）；② `test_keyword_service.py::test_lookup_literal_percent_and_underscore`（`100%`/`100 `、`a_b`/`a b` 四组不串号）；③ 真实 PG 集成 `test_keyword_dataset.py::test_m05_duplicate_new_words_same_sheet_pg`（`SessionLocal` 直跑：3 行落库、last-row-wins、`lookup_metrics` 字面量精确） | ✅ |

**B3 测试结果**：`453 passed, 1 skipped`（B2 后 448 + 新增 5：H04 全链路回归 ×1、M05 unit ×3、M05 PG 集成 ×1）。**无需新迁移**（无 schema 变更）。

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

- **2026-09-17 — B3 完成**：H04 / M05 全部修复并测试（`453 passed, 1 skipped`）。
  - **H04**：`prepare_keyword` 改 `async def`（orchestrator 统一 await，旧 sync 返回 `KeywordMetrics` 对已知词必 TypeError；未知词返回 None 侥幸通过）。新增 orchestrator 级已知词全链路回归（`test_p9_pipeline.py::test_known_keyword_full_run_reaches_ready`）。
  - **M05**：① 导入每 sheet 先 `casefold` 内存去重（后行覆盖 + warning），消除 `autoflush=False` 下重复新词 commit IntegrityError；② 全部关键词/簇名比较改 `lower()` 字面量精确匹配，`%`/`_` 不再当 LIKE 通配符。新增生产同款 `autoflush=False` session 测试 ×2、unit 字面量匹配 ×1、真实 PG 集成 ×1。
  - **无新迁移**（无 schema 变更）。
- **2026-09-17 — B2 完成**：H01 / H02 / M15 全部修复并测试（`448 passed, 1 skipped`）。
  - **H01**：DataForSEO 解析按官方 Live Advanced 契约重写（`result`=block 列表、`items[]` 扁平混合按 `type` 分派、rank 取 `rank_group`、PAA 嵌套 `people_also_ask_element`、related 字符串列表）；新增 **featured snippet**（`FeaturedSnippet` schema + `SerpResult.result_type='featured'` 落库，无需迁移）。`test_dataforseo_provider.py` 整文件重写（官方脱敏信封，23 用例）。
  - **H02**：Exa `extract()` 显式 `text: True`（API 默认 `false` → 否则无正文 SOURCE_EMPTY）。
  - **M15**：三家 provider `health_check()` 全部严格化，消除 401/403 假阳性——DataForSEO 付费 SERP + 业务 `status_code==20000` 校验；Exa 改零成本“非法 body 认证探测”（400/422=True，401/403=False）；Tavily 保留付费探测但要求 HTTP200 + `results`/`failed_results` 形状。
  - **M12 前置子项**：`_extract_cost()` 改读官方 `cost`（USD），弃用 `credits`（主体在 B7）。
  - `scripts/p2_acceptance.py` 改官方形状并对本地 PG 实跑 **PASS**（8 organic / 3 PAA / 1 featured 落库）。**无新迁移**。
- **2026-09-17 — B1 完成**：M04 / H08 / L02 / M09 / M11 全部修复并测试（`426 passed, 1 skipped`）；
  新增迁移 `0011_error_raw_usage_prompt`（`generation_jobs.error_raw` + `llm_usage` prompt 三列）；
  新增测试 `tests/unit/test_h08_rq_timeout.py`，扩展 `test_p9b1_cost.py` / `test_p9_reliability.py` /
  `test_p9b3_error_ui.py` / `test_generation_job.py`。H11 移入 B4。
