# 审计修复进度跟踪（CODEX Audit → 分批修复）

> 依据：`CODEX-AUDIT-REPORT.md`（审计 commit `c5c7101`，结论：整体验收不通过；
> 0 Critical / 12 High / 15 Medium / 3 Low，共 30 项）。
>
> 修复顺序遵循 AGENTS.md 的 P0 → P9 阶段约束与报告 §11 建议，
> 每批次完成后更新本文档并运行完整测试套件（`unit + integration`，PostgreSQL 本地实例）。
> 内容规则以 `SEO-AUTO-DEV-SPEC.md` 为最高优先级；不调用真实付费 API / 不写真实 Strapi，
> 全部使用 mock / fixture 验证。

> ## ⚠️ 诚实性更正（第二轮审计，R-L01）
>
> 本文件记录的 B1–B7 是**第一轮**修复。第一轮收尾时"30 项全部修复"的表述
> **超出实际证据**：第二轮 `CODEX-AUDIT-REPORT.md`（基于 commit `0636e2e`）
> 逐项复验后确认其中 **9 项只是"部分修复"**，并给出新编号 `R-H01`…`R-L01`。
>
> 下表逐项标注**当前真实状态**（第二轮 B8–B14 已全部收口，详见
> `docs/AUDIT-R2-FIX-PROGRESS.md` 与 `docs/audit-r2/` 各阶段详档）：
>
> | 第一轮项 | 第一轮记录 | 第二轮复验实际 | 第二轮批次 |
> |---|---|---|---|
> | H03 | 部分修复 | 部分修复（Blog 扁平已修，Upload 顶层数组仍崩）→ **R-H02** | B12 |
> | H06 | 关闭 | 实际未关闭（FAQ 空答案 / 有效 H1 / 旧 writer 审核）→ **R-H05** | B11 |
> | H08 | 关闭 | 实际回归（RQ 参数名错误）→ **R-H01** | B8 |
> | H09 | 部分修复 | 部分修复（只证明有正文）→ **R-H06** | B10 |
> | H11 | 部分修复 | 部分修复（审核历史仍被删除）→ **R-M03** | B11 |
> | H12 | 部分修复 | 部分修复（路径越界 + 失败覆盖 live 文件）→ **R-H07 / R-M01** | B13 |
> | M01 | 部分修复 | 部分修复（标准上传异常未落失败态；有行无 ID 重试重复）→ **R-H02 / R-H03** | B12 |
> | M11 | 部分修复 | 部分修复（dict raw 使失败处理再崩）→ **R-H04** | B9 |
> | M12 | 部分修复 | 部分修复（SERP/图片/证据抓取成本仍丢失）→ **R-M02** | B13 |
> | L03 | 关闭 | 实际未关闭（"全部修复"表述不准确）→ **R-L01** | B14 |
>
> 其余 21 项在第二轮复验中维持"已关闭"（报告 §6 逐项映射）。
> 因此：**本文件的 B1–B7 明细是历史记录，其"状态 ✅"只代表第一轮的证据范围；
> 最终状态以 `docs/AUDIT-R2-FIX-PROGRESS.md` 为准。**

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
| B4 | P4 / P5 | H09, H06, H07, H11, M08 | ✅ 完成（见下） |
| B5 | P6 | H05, H10, M06, M07, L01 | ✅ 完成（见下） |
| B6 | P7 | H03, M01, M02 | ✅ 完成（见下） |
| B7 | P8 / P9 + 文档 | M13, M14, M12, M10, M03, H12, L03 | ✅ 完成（见下） |

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

### B4 — P4 / P5 ✅

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| H09 | High | evidence 来源全部由同一 LLM 自述，无真实性核查，引用可能指向不存在的"研究" | 新增独立核实通道 `verify_evidence_sources()`：每条 note 的 `source_url` 由**内容提取器**（非产出 note 的同一 LLM）抓取；抓取失败/异常/空正文 → 副本降级 `confidence="low"` + `usage="avoid"` 并追加 `source_unverified:` 标记；重跑时整组替换（delete+reinsert）；无 verifier（focused unit）则原样通过。orchestrator 将配置的 extractor 注入 `run_evidence_research(verifier=...)`；日志新增 `downgraded_unverifiable_sources` | `app/services/evidence_verification.py`（新增）、`app/pipeline/steps/evidence_research.py`、`app/pipeline/orchestrator.py` | `tests/unit/test_research_steps.py` 新增 6 用例（无 verifier 原样 / 可达源保留 / 不可达降级 / 空正文降级 / 抓取异常降级 / brief 带 strategy）；`tests/integration/test_p9_pipeline.py`：全链路与 retry 断言计入证据核实调用（冷缓存 `TOP_N+1` 次抽取、缓存命中重跑仅 `[[EVIDENCE_SOURCE_URL]]`、强制刷新 `TOP_N+1` 且 URL 集合含证据源） | ✅ |
| H06 | High | DoD gate 形同虚设：FAQ/CTA 只查标题存在、Setext H1 不识别、图片不查文件是否存在、畸形/小写 marker 放过、无研究/审核行完整性 | `validate_article_done` 重写为基于 Markdown 结构解析的完整性门禁（仍为只读）：① 正文必须含 ATX 或 **Setext** 形式 H1（`_H1_PATTERN` + 下划线分隔行识别，排除 H2 误判）；② FAQ 段逐题计数且空答案不计、CTA slot 段必须有正文；③ 图片 plan 逐条核对 `local_path` 非空且文件存在；④ marker 大小写不敏感扫描（`_INTERNAL_LINK_ANY`）——未知/非激活内链、小写 `[[internal_link:x]]`、小写前缀一律 fail；⑤ 关联完整性：每源竞品分析、研究行（synthesis/brief/outline 非空且不损坏）、当前 writer 草稿的 3 份 review + 终稿 anticopy（serious overlap 亦 fail）、SERP run 存在、source 去重/上限、图片数量；⑥ **H11 新门禁**：最新版本必须 `stage == "revision"`（writer 重跑后 reviser 未重跑 → 裸草稿不再静默放行）。`article_field_failures` 负向 fixture 化（参数化 mutate + needle 断言错误信息） | `app/services/article_dod.py`、`app/schemas/article.py` | `tests/unit/test_article_dod.py` 大幅扩展（合规 job 过门、只读断言、字段级负向参数化、缺 review/anticopy/overlap、未知与小写 marker、缺 SERP、重复源、源上限、坏 outline/outline 空、坏 brief、Setext H1、H2 下划线不误判、FAQ 题数/空答案、CTA 空段、图片路径缺失/None、缺竞品分析、无版本） | ✅ |
| H07 | High | 最终导出（article.md）与 Strapi 同步只解析 `[[IMAGE:N]]`，writer 的 `[[INTERNAL_LINK:X]]` 原始 marker 被静默写入导出与 Draft | 新增共享终稿渲染器 `render_final_body()`：先解析内链（`resolve_markers`）再解析图片（`resolve_image_markers`），任一残留 marker 一律抛 `PipelineError`（DoD 应已拦截，到达此处即契约违例，必须响亮失败）；本地导出（`image_generate` 末步）与 `strapi_sync` 两条终稿路径统一改走该渲染器 | `app/services/final_body.py`（新增）、`app/pipeline/steps/image_generate.py`、`app/pipeline/steps/strapi_sync.py` | `tests/unit/test_image_steps.py` 新增 2 用例（导出解析内链 marker / 未知内链 marker 抛错）；`tests/unit/test_strapi_sync_step.py` 新增 2 用例（sync body 解析内链 / 未知 marker 失败）；DoD 侧 `test_unknown_internal_link_marker_fails`、`test_lowercase_internal_link_marker_fails` 兜底 | ✅ |
| H11 | High | retry 删除全部 article_versions/reviews，文章历史不可追溯；且"当前版本"靠删除来隐式表达 | **不可变 append-only 历史**（spec §27/§28/46.13–46.14）：① `checkpoints.reset_from_step` 与 `routes/jobs._reset_artifacts` **不再删除** versions/reviews，只删可重跑的 per-run 产物（SERP/来源/研究/outline/图片/LLM usage）；② "当前有效"**派生**而非删除：新增 `latest_writer_version()`（最新 `stage="writer"`）——reviewer/reviser 改评审/修订**当前 writer 草稿**而非全局最新版本；③ reviser `step_done` 新谓词：存在 `version >` 当前 writer 版本号的 revision（writer 重试追加 v3 后，stale revision v2 不再算完成，resume 自动重跑 10→13）；reviewer `step_done` 仍按 writer 草稿挂行（旧草稿的 review 属历史，不计）；④ DoD 新门禁：最新版本必须 `stage="revision"`。无效化时机=**新 writer 版本追加时**（非 reset 时）。无需迁移（无 attempt 列/无 schema 变更） | `app/pipeline/checkpoints.py`、`app/pipeline/steps/_article_common.py`、`app/pipeline/steps/reviewers.py`、`app/pipeline/steps/article_reviser.py`、`app/routes/jobs.py`、`app/services/article_dod.py` | `tests/unit/test_p9_reliability.py::TestCheckpoints` 重写/新增 3 用例：reset(9) 保留 v1+v2+review 仅删图片（first_incomplete=14）、reset(10) 保留全部版本历史、**新 writer 草稿 v3 使 stale revision 失效**（reviser/reviewer 转未完成，first_incomplete=10）；既有全链路/断点续跑断言（versions `[1,2]`、stages、review 超集）无需改动即通过 | ✅ |
| M08 | Med | `job.strategy` 未进入 Brief prompt（LLM 自造角度）；writing/review 温度硬编码，`.env` 调参无效 | ① `content_brief.build_user_prompt` 新增 `strategy` 参数并附 `STRATEGY_MEANINGS` 释义（auto/high_volume/low_kd/high_cpc/long_tail/pillar 六种策略语义入 prompt），`run_content_brief` 传 `job.strategy`；② 温度全部改读 Settings：writer `llm_temperature_writing`、reviser 新增 `llm_temperature_revision=0.50`（原硬编码 0.5）、image_plan 新增 `llm_temperature_image_planning=0.30`；review 各步沿用 `llm_temperature_review` | `app/core/config.py`、`app/pipeline/steps/content_brief.py`、`app/pipeline/steps/article_writer.py`、`app/pipeline/steps/article_reviser.py`、`app/pipeline/steps/image_plan.py`、`prompts/content_brief.md`（strategy 字段说明） | `tests/unit/test_article_steps.py::test_m08_temperatures_come_from_settings`（monkeypatch Settings 后 writer/reviser/image_plan 各取各自温度）；`tests/unit/test_research_steps.py::test_content_brief_prompt_carries_job_strategy`（strategy 入 prompt 含语义） | ✅ |

**B4 测试结果**：`478 passed, 1 skipped`（B3 后 453 + 新增 25：H09 ×6、H06 扩展、H07 ×4、H11 ×3、M08 ×2（含 image_plan 温度用例），含改写用例）。**无需新迁移**（H11 刻意不引入 attempt 列——当前版本由历史派生）。

### B5 — P6 ✅

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| H05 | High | image step 提前 commit `ready`，DoD gate 在其后；sync 入口无状态门禁 | ① `image_generate` 不再置 `ready`（step 结束于 `image_generating`）；② READY 仅由 orchestrator 在 `validate_article_done` 通过后**同事务**设置（`completed_at` 取既有值或 now），fresh/retry/resume/no-rework backfill 四种路径统一；③ `POST /jobs/{id}/sync-strapi` 服务端门禁：status ∉ {ready, strapi_draft_created, strapi_syncing} → **409**（failed/cancelled 的 body 未过 DoD，不得进 CMS）；④ UI `can_push` 镜像同一门禁（去掉 failed） | `app/pipeline/steps/image_generate.py`、`app/pipeline/orchestrator.py`、`app/routes/jobs.py` | `tests/unit/test_image_steps.py`：4 处 READY 断言改 `IMAGE_GENERATING`；`tests/integration/test_image_pipeline.py` 直接驱动 step 后断言 `image_generating`；`tests/unit/test_p8_routes.py` 新增 4 用例（ready→enqueue / failed→409 / cancelled→409 / queued→409 / draft_created 重推→200）；既有全链路 p8/p9 断言 `ready` + 完整 DoD 不变 | ✅ |
| H10 | High | 图片下载向任意响应 URL 带 Images API key（外域 CDN 泄露凭证） | `_download()` 去掉 `Authorization` 头——Images API key 只认证生成端点；响应 URL（同源/第三方 CDN/预签名）一律匿名下载；生成端点/health_check 的 Bearer 保持不变 | `app/providers/image/openai_image.py` | `tests/unit/test_openai_image_provider.py` 新增 `test_download_never_sends_api_key_to_response_url`（同源/跨域/presigned 三目标逐一断言下载请求 `Authorization is None`）+ `test_download_404_is_provider_failed` | ✅ |
| M06 | Med | 图片重试全部重新生成已成功图片 | `run_image_generation` 生成循环前校验 row 的 `local_path`：文件存在且通过 M07 校验（可解码 + 扩展名一致）→ **复用**（不产生 provider 请求），仅缺失/损坏/格式不符的行重新生成；复用清单写 `image_generation_reused_existing` 日志 | `app/pipeline/steps/image_generate.py` | `tests/unit/test_image_steps.py::test_generation_failure_keeps_image_generating` 断言重跑仅 2 次请求（hero 复用，两个 inline 重做）；新增 `test_m07_corrupt_or_mismatched_file_regenerates`（`.webp` 内写入 PNG 字节 → 重跑必重生成并转回真 WebP） | ✅ |
| M07 | Med | `.webp` 文件名实际保存 PNG/JPEG 字节 | 新增 `app/services/image_storage.py` 图像契约层：① `save_image_bytes` 对 `.webp` 文件名**转码为真 WebP**（Pillow，PNG/JPEG→WEBP q85）后落盘，返回真实 MIME；② `validate_image_bytes` = 魔数嗅探 + Pillow 解码校验 + 扩展名/MIME 一致性（`.webp` 含 PNG 字节 → `IMAGE_PROVIDER_FAILED`）；③ `validate_local_image` 供 M06 复用前校验；④ provider 落盘前置校验改走同一函数（删除本地弱嗅探）；`pyproject.toml` 新增 `pillow>=10.0`；测试 fixture 全部换成**真实可解码** 1×1 PNG（旧 fake PNG 魔数+假 IDAT 过不了解码校验） | `app/services/image_storage.py`（重写）、`app/providers/image/openai_image.py`、`pyproject.toml`、`tests/unit/test_openai_image_provider.py` | 新增 `tests/unit/test_image_steps.py::test_m07_webp_filename_gets_real_webp_bytes`（PNG payload → 落盘 RIFF/WEBP 魔数 + `image/webp`）+ `test_m07_corrupt_or_mismatched_file_regenerates`；`test_generate_stores_file_section34` 断言 `.webp` 落盘字节为真 WebP 且 mime 一致；全链 p8/p9 测试通过 | ✅ |
| L01 | Low | 本地 artifact 目录缺 spec §34 列出的研究 JSON | 新增 `app/services/job_artifacts.py::export_research_artifacts`：从 DB 读 content-brief/outline/serp（含 synthesis）/reviews/sources（含 evidence notes），写入 `content-brief.json` / `outline.json` / `serp.json` / `review.json` / `sources.json`；`image_generate` 导出块末尾调用——§34 目录成为自包含离线交付物；缺失行组写空 payload（显式缺失，不抛错） | `app/services/job_artifacts.py`（新增）、`app/pipeline/steps/image_generate.py` | `tests/integration/test_p9_pipeline.py::test_known_keyword_full_run_reaches_ready` 新增 7 个 artifact 存在性 + 非空断言（serp.runs/results、review.reviews、sources.sources、brief 非空）；`test_p8_pipeline.py` 全链路同款 7 项断言；`test_image_pipeline.py` 直接驱动路径 5 项断言；p8/p9 cleanup 同步清理新 JSON | ✅ |

**B5 测试结果**：`486 passed, 1 skipped`（B4 后 478 + 净增 8：H05 路由门禁 ×4、H10 ×2、M06/M07 ×2；另有若干既有断言按新语义改写）。**新增依赖 `pillow>=10.0`**（WebP 转码/解码校验）；**无新迁移**。

### B6 — P7 ✅

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| H03 | High | Strapi 5 扁平 data / 数组上传 / relation populate 契约不兼容 | **Strapi 5 REST 契约解析**（§35 兼容 v4 兜底）：① `_entry_from_item()` 扁平优先——`data` 直接取 `id/documentId/title/...`，无 `id/documentId` 报 `STRAPI_SCHEMA_MISMATCH`；嵌套 `data` dict 与 `attributes` 包裹（v4）均可兜底解析；② `_upload_result()` 接受 Strapi 5 的 `data` **数组**（取 `[0]`）与 v4 单对象（含 `data.data` 包裹）；③ `get_draft` 增加 `populate[author]=*&populate[category]=*&populate[mainImage]=*` 验证读取；④ schema 修正：`author/category` 放宽为 `str|int|dict|None`（documentId 短串 / 数字 id / populated 对象）、`main_image` 放宽 `str|dict|None`；新增 `relation_document_id()`（三种形态统一提取 documentId，`None` 保持 `None`=不比对）、`media_url()` / `media_id()` helper；⑤ `routes/strapi.py::_items` 扁平优先标签/键查找（v4 容忍） | `app/providers/cms/strapi_cms.py`、`app/schemas/strapi.py`、`app/routes/strapi.py` | `tests/unit/test_strapi_cms_provider.py` 整改：`_blog_item` 改扁平形状（populated relation 默认值），新增 `_blog_item_v4` 与 `_upload_response(v4=)`（默认数组）；新用例 ×4：v4 形状容忍（含 `populate%5Bauthor%5D` query 断言）、populated relation 解析、v4 单对象上传、数组缺 id → schema mismatch。`test_strapi_sync_step.py` 全 fake 改 Strapi 5 形状（`_doc_view` 扁平 + `get_override`），新用例 `test_verify_accepts_populated_relation_objects`（populated author/category dict 通过） | ✅ |
| M01 | Med | sync 失败后 job 停 `strapi_syncing`，UI 无错误区/重试入口 | **统一持久化 `strapi_sync_failed` 状态**（§64）：① `JobStatus` 新增 `strapi_sync_failed`（**非** terminal——`is_terminal` 仍仅 ready/failed/cancelled，pipeline 级 retry/cancel 对同步失败 job 维持 409；§8 枚举无此值，以 §64 失败语义为准，决策已记录）；② `run_strapi_sync` 三条失败路径全部落统一状态——**pre-sync 前置检查失败**（`_fail_pre_sync`：无 row 时也创建 FAILED sync row，保留 `error_code/message`）、**中途失败**（`except PipelineError`：row 为 None 时先按 job_id 重查再创建，绝不双插——`UNIQUE(job_id)` 锚点）、**缺本地图片文件**（`_read_image_bytes` OSError → `STRAPI_UPLOAD_FAILED`）；失败时 job 置 `strapi_sync_failed` + `current_step` + error 字段 + `completed_at`，sync row 置 FAILED 并**保留 `strapi_document_id`**（§41 幂等重试锚）；③ 重试走 sync 专用通道：`_SYNCABLE_STATUSES` / `can_update` 纳入 `strapi_sync_failed`（UI Push/Update 按钮 + `POST /jobs/{id}/sync-strapi`），`ready` 进度列表 15/15 含该状态；④ 清理永不扫：`strapi_sync_failed` 不在 `AUTO_DELETABLE_STATUSES`/`TERMINAL_STATUSES`；⑤ UI：`job_fragment.html`/`jobs.html` 徽章 `badge-error`（`.badge-error` CSS 新增），Strapi 卡片 "Sync failed" 告警区展示 error 字段 | `app/core/enums.py`、`app/pipeline/steps/strapi_sync.py`、`app/routes/jobs.py`、`app/templates/job_fragment.html`、`app/templates/jobs.html`、`app/static/style.css` | `tests/unit/test_strapi_sync_step.py`：4 个既有失败断言改 `strapi_sync_failed`（slug-conflict 现断言 row 存在且 FAILED、`strapi_document_id is None`）；新用例 ×5：`test_sync_failed_state_is_not_terminal`、`test_missing_local_file_fails_with_upload_error`、`test_mid_failure_then_retry_updates_same_draft`（中途失败后重试更新同一 draft、零 create）、`test_verify_accepts_absolute_url_main_image`、重试测试 server 状态显式携带已有 mainImage。`test_p8_routes.py` 新 ×2：`strapi_sync_failed` 同步重试 200+enqueue、retry 409 但 cancel 200（用户可放弃）。`test_p9c_ops.py`：清理用例改用 `strapi_sync_failed` + 陈旧 `completed_at`（断言永不自动清扫）。`test_strapi_pipeline.py`（PG 集成）：中途失败断言改 `job.status == strapi_sync_failed` | ✅ |
| M02 | Med | GET 验证只查非空，不核对字段值与 Draft 状态 | **STEP F 逐字段值校验**：GET verify 从"非空检查"升级为规范化值比对——`id`/`documentId` 与 row 持久化值一致、`status == "draft"`（`published` 等一律 fail 并明示实际状态）、`title == doc.title`、`slug == doc.slug`、`body == final_body`（渲染后终稿）、`metaTitle/metaDescription` 与 doc 字段一致、`seoKeywords == build_seo_keywords(...)`（与创建/更新写入同一规范化函数，12 个 / 500 字符上限）、author/category 经 `relation_document_id()` 与期望 documentId 比对（**仅当期望值非 None**）、mainImage 走 `_url_path()` **URL 路径**比对（本地持久化绝对 public URL vs Strapi 存储相对/绝对原始值），`hero.strapi_media_id` 存在时再核 `media_id()`。所有问题合并为单条 `PipelineError(STRAPI_SCHEMA_MISMATCH, "GET verify failed after sync: …")` | `app/pipeline/steps/strapi_sync.py` | `tests/unit/test_strapi_sync_step.py` 新用例 ×5：`test_verify_rejects_wrong_title`（错误 title → fail 且保留 docId）、`test_verify_rejects_published_status`、`test_verify_rejects_wrong_relation_document_id`（author 换成他人 documentId → fail）、`test_verify_rejects_wrong_media`（mainImage 指向别的图 → fail）、`test_verify_accepts_populated_relation_objects`（populated dict 形态不误报） | ✅ |

**B6 测试结果**：`501 passed, 1 skipped`（B5 后 486 + 净增 15：provider ×4、sync step ×9、p8 routes ×2；另有多条既有断言按 Strapi 5 扁平/数组形状与 `strapi_sync_failed` 新语义改写）。**无新迁移**（无 schema 变更；`strapi_sync_failed` 是 status 枚举值，落 `generation_jobs.status` varchar）。

> **决策记录（§8 vs §64）**：§8 规范状态枚举未列 `strapi_sync_failed`，§64 明确"Strapi 同步失败"为独立失败语义。取 **21 值枚举**（含 `strapi_sync_failed`），且该值**不是** terminal——文章 pipeline 已成功（§41 锚点 `strapi_document_id` 必须保留以供幂等重试），只有 sync 专用通道可重试；pipeline 级 retry/cancel 与自动清理均不触碰它。`test_enums.py` 断言 21 值 + 非 terminal，防止后续漂移。

### B7 — P8 / P9 + 文档 ✅

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| M13 | Med | Job Detail 无 competitor/synthesis/evidence 研究正文与 Logs 区 | `job_detail_payload` 读时派生（无 schema 变更）：`competitor_analyses`（逐条 + 源页 title/url/analysis/model）、`synthesis`（最新 SerpSynthesisRow）、`evidence_notes`（claim/source/type/confidence/usage/note）、`logs`（`current_step` + job error code/message/raw + 全 15 步 `checkpoint_status`）；`job_fragment.html` 新增 4 卡片（Competitor Analyses / SERP Synthesis / Evidence Notes / Logs：当前步 + 错误 + 15 步进度 ol） | `app/routes/jobs.py`、`app/templates/job_fragment.html` | `tests/unit/test_p8_routes.py::test_web_job_detail_research_and_logs`（直接调 `job_detail_payload` 断言 4 字段 + fragment HTML 含 4 标题与内容） | ✅ |
| M14 | Med | enqueue 失败被当正常 queued，UI 无恢复提示 | ① `create_job`：`_enqueue` 返回 False 时 job 保持 `queued` 并写 `error_code="ENQUEUE_FAILED"` + 中文 error_message（非 terminal，符合 §43.3 降级到手动重试）；② `CreateJobResponse` 增 `enqueued: bool = True` / `error: str \| None = None`（向后兼容）；③ 新增 `POST /api/jobs/{id}/enqueue`（**仅** `status=="queued"` 可重投，拒绝 terminal 与非 queued → 409，成功后清 error 三字段）；④ `job_fragment.html` 在 `queued + ENQUEUE_FAILED` 时显示错误告警 + "重新入队" `hx-post` 按钮 | `app/routes/jobs.py`、`app/schemas/job.py`、`app/templates/job_fragment.html` | `tests/unit/test_p8_routes.py::test_m14_enqueue_failure_degrades_and_recovers`（monkeypatch `_enqueue` False → API `enqueued=False`+error、row 有 `ENQUEUE_FAILED`、fragment 显示入队失败 + 重入队按钮；恢复 `_enqueue`→True 后重入队 200、标记清除、仍仅 1 个 job 行）+ `test_m14_re_enqueue_rejects_running_job`（`serp_searching` → 409） | ✅ |
| M12 | Med | 成本随重试删除/覆盖、缓存归属错误（读 credits 而非 cost） | ① `llm_usage` 改**不可变 append-only 台账**（§54）：`checkpoints.reset_from_step` 删除 L363-391 的 `delete(LLMUsageRow)` 块——重试只追加新行、累加成本，不删历史（无读者按"当前 usage"求和，安全）；`llm_usage.py` model docstring 同步更正；② `source_extract`：`source_pages` 是跨 job 共享 TTL 缓存，`provider_cost` = 该页**最后一次 fresh 提取**的成本（非每 job 台账）——cache hit 路径从不改写行，零新增成本（注释明确化）；③ `image_generate`：复用（M06）路径零新增，仅真实重生成写入**当前**成本；④ `_extract_cost()` 读 `cost` 非 `credits`（B2 已改，本批核对）；⑤ None 成本保持 None（不造 0） | `app/pipeline/checkpoints.py`、`app/db/models/llm_usage.py`、`app/pipeline/steps/source_extract.py`、`app/pipeline/steps/image_generate.py` | `tests/unit/test_p9b1_cost.py::TestResetKeepsUsageLedger`（改写原 `TestResetDeletesUsage`：reset 不删任何 usage 行 / 全量 reset 保留 + 重跑累加 3 行 in=42 / None tokens 不造 0）+ `tests/unit/test_p9_reliability.py::TestM12CacheHitCost::test_cache_hit_does_not_repay_or_fabricate_cost`（fresh 提取 cost=0.25 落缓存行；cache hit 0 次调用且不改写行；None 成本经 force_refresh 落 None） | ✅ |
| M10 | Med | JSON 序列化 secret 与异常消息脱敏不可靠 | 新增 `app/core/redaction.py`：`redact()`（Bearer → KV → URL-query 顺序，`<redacted>` 替换）/ `redact_value()`（递归 dict/list/tuple/set）/ `redact_dict()`；`JsonLogHandler.emit` 对 event、extra 非内建字段、异常文本、最终 JSON 行全部脱敏；orchestrator `PipelineError` + UNEXPECTED 分支脱敏 `error_message`/`error_raw`/traceback；`strapi_sync` 两条失败路径脱敏（token 不进日志/web，§60） | `app/core/redaction.py`（新增）、`app/core/logging.py`、`app/pipeline/orchestrator.py`、`app/pipeline/steps/strapi_sync.py` | `tests/unit/test_logging.py`（9 用例：Bearer/KV/URL-query 替换、递归嵌套、extra 非内建字段、异常文本、handler 端到端 JSON 行、`redact_dict`、非 dict 容忍、幂等、无 secret 不变） | ✅ |
| M03 | Med | Preview 输出原始 Markdown，缺 inline 图片 | ① 渲染器改 `MarkdownIt("default")` + `linkify=False`（**非** commonmark——后者透传原始 HTML）：转义原始 HTML（`<script>`→`&lt;script&gt;`）与属性值（`&quot;`/`%22`）、渲染 GFM 表格、绝不发 `javascript:` 链接（保留字面文本）→ 输出可安全 `\|safe`；② 新增 `_local_image_url(job,row)`（`local_path` 文件存在 → `/static/job-images/{job.id}/{filename}`）与 `_resolve_preview_body(session,job,version)`：`resolve_markers`（未知 marker 保留字面 → 容错）→ 查 `ImageRow`（sort_order）→ hero = 首个 role=="hero" → inline 图仅本地文件存在时按 `insertion_marker` 内联 → `insert_image_markers` → `resolve_image_markers` → `_render_md`；③ preview 路由传 `hero_alt` | `app/routes/web.py`、`app/templates/article_preview.html` | `tests/unit/test_p8_routes.py` 新增 3 用例：`test_web_article_preview_renders_markdown`（h2+表格渲染、原始 `|-------|` 不出现、`alt="Hero caption"`，写真实 tmp 图片文件）、`test_web_article_preview_escapes_malicious_html`（`&lt;script&gt;` 出现、活标签不出现）、`test_web_article_preview_inline_image_marker`（img+alt 内联、marker 已解析） | ✅ |
| H12 | High | 备份含图片但 restore 不恢复图片 | ① `restore_archive(archive, target_engine, *, clear=False, data_dir=None) -> {"tables","artifacts","image_files","missing_images"}`：**写库前**解包 `artifacts.tar` 到 `{data_dir}/.restore_staging` → 校验 image 行的 `local_path` 重锚后文件齐全（缺失 → `BackupError`，**DB 完全不动**）→ 提升为 `{data_dir}/articles` → 单事务导入 DB；② `validate_archive`：image 行>0 但归档无 artifacts → 报错（OSError/tar/json/ValueError 包装为 `BackupError`）；③ `_remap_local_path` 重锚 `articles/` 段；④ CLI `restore` 增 `--data-dir` | `app/ops/backup.py` | `tests/unit/test_p9c_ops.py` 新增 5 用例（归档布局含 artifacts / 写库前缺文件 → BackupError 且目标库零行 / 全新 data_dir round-trip 图片文件落位 + DB 行路径重锚 / image 行无 artifacts → 校验报错 / staging 清理）+ 既有 restore round-trip 断言扩展 | ✅ |
| L03 | Low | README/PHASE-LOG “全部实现完成”口径高于实际 | ① `README.md` "Current phase" 段改写：P0–P9 实现 + 审计 B1–B7 全部修复，逐批 **commit 表**（B1 `cf85665` … B6 `52d3643` / B7 HEAD）+ 诚实口径声明——测试口径（fake providers + 本地 PG，不调真实付费 API / 不写真实 Strapi）vs live 运行验收（真实服务跑通整篇 READY + 人工抽查，**独立上线前步骤，尚未完成**）；测试基线注更新为 `520 passed`；② `docs/PHASE-LOG.md`：总览表增审计行（含 7 个 commit）、总览段诚实口径声明、新增"B1–B7 审计修复"详节（逐批修复项 + 测试基线演进 415→520） | `README.md`、`docs/PHASE-LOG.md` | 静态核对（文档）；与 `git log` 7 个批次 commit 一一对应 | ✅ |

**B7 测试结果**：`520 passed, 1 skipped`（B6 后 501 + 净增 19：M03 ×3、M13 ×1、M14 ×2、M12 新增 ×3 + 改写 ×2、M10 ×9、H12 ×5；另有多条既有断言按 M12 台账语义改写）。**无新迁移**（M13 读时派生、M14 复用现有列）。

## 变更日志

- **2026-09-17 — B7 完成**：M13 / M14 / M12 / M10 / M03 / H12 / L03 全部修复并测试（`520 passed, 1 skipped`，净增 19 用例）。**无新迁移**。至此 CODEX 审计报告 30 项（0 Critical / 12 High / 15 Medium / 3 Low）全部在 B1–B7 收口。
  - **⚠️ 更正（第二轮 R-L01）**：本行"30 项全部收口"的表述不成立。第二轮复验确认
    其中 9 项只是"部分修复"（见文首诚实性更正表），并已在 B8–B14 逐项真正收口；
    最终状态以 `docs/AUDIT-R2-FIX-PROGRESS.md` 为准。
  - **M13**：Job Detail 读时派生（无 schema 变更）研究正文 + Logs——`competitor_analyses`（逐条 + 源页 title/url/analysis/model）、`serp_synthesis`、`evidence_notes`、`logs`（current_step + job error + 全 15 步 checkpoint）；`job_fragment.html` 新增 4 卡片。
  - **M14**：enqueue 失败降级 + 安全重投——入队失败 job 保持 `queued` + `error_code="ENQUEUE_FAILED"`（非 terminal，§43.3 降级手动重试）；`CreateJobResponse` 增 `enqueued/error`；新增 `POST /jobs/{id}/enqueue`（仅 `status=="queued"` 可重投，其余 409，成功后清 error 标记）；fragment 红色告警 + "重新入队" `hx-post` 按钮。
  - **M12**：`llm_usage` 改**不可变 append-only 台账**（§54）——`reset_from_step` 不再删 usage 行（重试累加、保留历史）；`source_pages` 共享缓存 `provider_cost` = 最后一次 fresh 提取成本（cache hit 零新增）；`image_generate` 复用零新增；`_extract_cost` 读 `cost` 非 `credits`（B2 已改）；None 成本保持 None（不造 0）。
  - **M10**：新增 `app/core/redaction.py`（Bearer → KV → URL-query 顺序递归脱敏）；`JsonLogHandler` / orchestrator 异常路径 / strapi_sync 失败路径全部脱敏，secret 不进日志/web（§60）。
  - **M03**：预览安全渲染 + 图片 placement——`MarkdownIt("default", linkify=False)`（转义原始 HTML/属性值、GFM 表格、不发 `javascript:` 链接）；`_resolve_preview_body()` 内链 → hero → inline（仅本地文件存在）→ 渲染；fragment 传 `alt`。
  - **H12**：`restore_archive` 写库前解包 `artifacts.tar` 并校验图片文件齐全（缺失 → `BackupError`，DB 不动），重锚 `articles/` 后单事务导入；`validate_archive` 对"有 image 行但无 artifacts"报错；CLI 增 `--data-dir`。
  - **L03**：诚实文档——`README.md` 现状段改 B1–B7 逐批 commit 表 + mock/live 验收边界声明；`docs/PHASE-LOG.md` 总览表增审计行、新增 B1–B7 详节、测试基线更新至 `520 passed`。
- **2026-09-17 — B6 完成**：H03 / M01 / M02 全部修复并测试（`501 passed, 1 skipped`，净增 15 用例）。**无新迁移**。
  - **H03**：`StrapiCMSProvider` 解析改 Strapi 5 REST 契约——entry 扁平 `data`（无 `attributes` 包裹，`id/documentId` 必填否则 `STRAPI_SCHEMA_MISMATCH`）、`/api/upload` 的 `data` **数组**取 `[0]`、`get_draft` 带 `populate[author]/* [category]/* [mainImage]/*`；v4 嵌套/单对象形状全部保留为兜底（§35）。schema 放宽 relation/media 为 `str|int|dict|None`，新增 `relation_document_id()/media_url()/media_id()` 统一 helper；`routes/strapi.py::_items` 扁平优先。
  - **M01**：新增非 terminal 状态 `strapi_sync_failed`（§64）——pre-sync 前置检查 / 中途失败 / 缺本地图片文件三条路径全部落统一持久化状态（job + sync row FAILED，**保留 `strapi_document_id`** 幂等锚）；同步重试走 sync 专用通道（`_SYNCABLE_STATUSES`/UI Push-Update/`can_update`），pipeline retry/cancel 维持 409，自动清理永不扫；UI 红色 `badge-error` 徽章 + Strapi 卡片 "Sync failed" 告警区。§8 枚举缺此值 → 以 §64 为准取 21 值（决策已记录，`test_enums.py` 防漂移）。
  - **M02**：STEP F GET verify 从非空检查升级为**逐字段值比对**——id/documentId/draft 状态/title/slug/终稿 body/metaTitle/metaDescription/`build_seo_keywords` 规范化 keywords/relations（`relation_document_id`，仅期望非 None 时比对）/mainImage（URL 路径比对 + `strapi_media_id` 存在时再核 media id），问题合并为单条 `STRAPI_SCHEMA_MISMATCH`。
- **2026-09-17 — B5 完成**：H05 / H10 / M06 / M07 / L01 全部修复并测试（`486 passed, 1 skipped`，净增 8 用例）。**新增依赖 `pillow>=10.0`**；**无新迁移**。
  - **H05**：`ready` 状态归属改到 orchestrator——image step 不再提前 commit `ready`；`validate_article_done` 通过后**同事务**置 `ready` + `completed_at`（覆盖 fresh/retry/resume/no-rework backfill 全部路径）。`POST /jobs/{id}/sync-strapi` 加服务端状态门禁（非 syncable 状态 409），UI `can_push` 镜像同一门禁（failed 不再可推）。
  - **H10**：`OpenAIImageProvider._download()` 去掉 `Authorization` 头，Images API key 不再随响应 URL（第三方 CDN / 预签名）外泄。
  - **M06**：image step 重跑复用已通过 M07 校验的 `local_path` 文件，仅重新生成缺失/损坏/格式不符的行（付费生成不重做）。
  - **M07**：`image_storage` 增加图像契约层——`.webp` 文件名强制真 WebP（Pillow 转码）+ 魔数/解码/扩展名一致性校验；provider 落盘前与 step 复用前共用同一校验；测试 fixture 的假 PNG 全部换成真实可解码 PNG。
  - **L01**：新增 `job_artifacts.py::export_research_artifacts`，§34 目录补全 `content-brief.json` / `outline.json` / `serp.json` / `review.json` / `sources.json`，本地导出成为自包含离线交付物。
- **2026-09-17 — B4 完成**：H09 / H06 / H07 / H11 / M08 全部修复并测试（`478 passed, 1 skipped`，净增 25 用例）。**无新迁移**。
  - **H09**：新增 `app/services/evidence_verification.py`——evidence note 的来源 URL 由独立提取器通道核实，不可达/空正文 → `usage="avoid"` + `confidence="low"` + `source_unverified:` 标记；orchestrator 注入 extractor，p9 集成测试的抽取调用计数同步计入该核实调用。
  - **H06**：DoD gate 重写为 Markdown 结构解析完整性门禁（Setext H1、FAQ 逐题计数、CTA 正文、图片文件存在性、marker 大小写不敏感、研究/审核/竞品分析关联完整性、源去重与上限）；负向 fixture 参数化。
  - **H07**：新增 `app/services/final_body.py::render_final_body`——内链→图片两级解析 + 残留 marker 响亮失败；本地导出与 Strapi 同步两条终稿路径统一走共享渲染器。
  - **H11**：文章 versions/reviews 改为**不可变 append-only 历史**——reset/retry 不再删除，当前草稿由 `latest_writer_version()` 派生；reviser `step_done` = 存在更新于当前 writer 草稿的 revision；DoD 新增"最新版本必须为 revision 阶段"门禁（writer 重试后未重跑 reviser 不再静默放行裸草稿）。
  - **M08**：`job.strategy`（含六种策略语义）贯通进 Brief prompt；writer/reviser/image_plan 温度改读 Settings（新增 `llm_temperature_revision` / `llm_temperature_image_planning`，`image_plan` 原硬编码 0.3 已改读 Settings）。
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
