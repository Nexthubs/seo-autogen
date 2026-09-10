# Phase Log(P0–P9 阶段任务与完成记录)

> 目的:让后续阶段/接手者无需翻聊天记录即可快速恢复上下文。
> 权威依据:`SEO-AUTO-DEV-SPEC.md`(工程规范)+ `prompts/seo_article_guideline.md`(内容规则)。
> 状态标记:✅ 完成 · 🚧 进行中 · ⬜ 未开始

## 总览

| 阶段 | 主题 | 状态 | 核心交付 |
|---|---|---|---|
| P0 | Skeleton / Infrastructure | ✅ | 项目骨架、配置、DB 会话、Alembic、健康检查、日志 |
| P1 | Core Models + Provider Framework | ✅ | 全部 ORM 模型、provider 抽象(LLM/SERP/Extractor/Image/CMS)、重试策略 |
| P2 | DataForSEO + Source Extraction | ✅ | SERP 搜索步骤、来源抓取、SourcePage TTL 缓存 |
| P3 | Keyword Dataset + Internal Links | ✅ | 关键词数据集(导入/查询)、站内链接服务 |
| P4 | Research Pipeline | ✅ | 竞品分析、SERP 综合、证据研究、内容简报、大纲(外部 prompt 文件) |
| P5 | Article Generation + QA | ✅ | 写稿、SEO/事实/风格三审、修订、结构化输出容错 |
| P6 | Image Pipeline | ✅ | 图片规划、生成、存储、正文标记解析、预览 |
| P7 | Strapi Integration | ✅ | Draft 同步(仅 Draft,发布永远手动)、slug、图片上传 |
| P8 | Web UI | ✅ | 全部 Web 页面 + API、HTMX 轮询、cancel/retry、文章预览 |
| P9 | Reliability / Production Hardening | ✅ | P9-A ✅ / P9-B ✅(B1 ✅, B2 ✅, B3 ✅)/ P9-C ✅ |

---

## P0 — Skeleton / Infrastructure ✅

**任务与结果**

- 项目目录按 spec §55 建立;FastAPI 应用(`app/main.py`)、Pydantic v2 配置(`app/core/config.py`,所有 URL/key/超时集中管理)。
- PostgreSQL 连接 + SQLAlchemy 2.0 sync Session(`SessionLocal`,`expire_on_commit=False`);Alembic(`migrations/`)。
- Redis + RQ 队列骨架;结构化日志(`app/core/logging.py`,event/duration_ms/error_code)。
- `GET /health` 检查 database + redis。
- 测试基线:pytest(9)+ pytest-asyncio(auto mode),集成测试连真实 PostgreSQL,`check_database()` 不可达时 skipif。

**Migration**:`0001_initial_baseline`

---

## P1 — Core Models + Provider Framework ✅

**任务与结果**

- 全部 ORM 模型就位(现 10 个模型文件:job/serp/source/research/article/images/keyword/internal_link/strapi_syncs)。
- Provider 抽象与实现:
  - LLM:`app/providers/llm/openai_compatible.py` — OpenAI 兼容 chat completions,纯 httpx;重试策略 429/5xx/timeout 退避(2s/5s/15s,LLM 2 次重试),401/400 不重试;结构化输出 JSON 容错 + 最多 2 次修复(spec §49)。
  - SERP:`app/providers/serp/dataforseo.py` — 含 `_extract_cost`(credits 解析)。
  - Extractor:`app/providers/extractor/exa.py`(Exa,重试策略同上)。
  - Image:`app/providers/image/`(OpenAI 兼容 Images API)。
  - CMS:`app/providers/cms/`(Strapi)。
- 统一异常 `PipelineError` + `ErrorCode` 枚举(spec §52)。

**Migration**:`0002_generation_jobs`

---

## P2 — DataForSEO + Source Extraction ✅

**任务与结果**

- Step 2 `serp_search`:DataForSEO 搜索 → `SerpRun`(含 raw_response 全量持久化,spec §12.2)+ `SerpResult`(organic,取前 8)。
- Step 3 `source_extract`:top-5 选取、内容去重、`JobSource` 关联。
- `SourcePage` TTL 缓存(`app/services/source_cache.py`,默认 168h,`find_fresh`/`upsert`,key=url_hash)。
- URL 归一化 + slug 服务。

**Migration**:`0003_serp_sources`

---

## P3 — Keyword Dataset + Internal Links ✅

**任务与结果**

- 关键词数据集:导入(`POST /keywords/import`,CSV)、`keyword_service`(查询/指标)。
- Step 1 `keyword_prepare`:关键词指标可用性判定(`keyword_metrics_available`)。
- 站内链接服务(`internal_link_service`)+ 文章渲染器(`article_renderer`,标记解析/内部链接插入)。

**Migration**:`0004_keyword_dataset`

---

## P4 — Research Pipeline ✅

**任务与结果**

- Step 4 `competitor_analysis`:每来源 1 次 LLM 分析(5 次),`MAX_COMPETITOR_CHARS=24000`,per-source 幂等(已分析来源跳过)。
- Step 5 `serp_synthesis` / Step 6 `evidence_research` / Step 7 `content_brief` / Step 8 `outline`(+`outline_repair` 修复 prompt)。
- Prompt 外置(spec §47/48):`prompts/*.md` + front matter(name/version),`prompt_service.load_prompt` 计算 SHA256;每个 LLM 结果行记录 `prompt_name`/`prompt_version`。
- 大纲校验器(`outline_validator`)。

**Migration**:`0005_research_pipeline`

---

## P5 — Article Generation + QA ✅

**任务与结果**

- Step 9 `article_writer`:写稿(版本 v1,stage=writer)。
- Step 10–12 `reviewers`(seo/fact/style 三审,基于 writer 版本)。
- Step 13 `article_reviser`:修订(版本 v2,stage=revision);anti-copy 检查在草稿+终稿各跑一次(见 P9-A)。
- 文章版本模型 `ArticleVersionRow`(version/stage/body_markdown/seo_title/meta_description/slug)。

**Migration**:`0006_article_pipeline`

---

## P6 — Image Pipeline ✅

**任务与结果**

- Step 14 `image_plan`:图片规划(数量/角色/位置/alt/prompt)。
- Step 15 `image_generate`:生成 + 本地存储(`data/articles/{job_id}/`)。
- 图片数量服务(`image_count_service`)、正文图片标记(`image_markers`)、存储(`image_storage`)。
- 品牌视觉规范外置 `prompts/brand_visual_guideline.md`(spec §32/47)。
- 该步骤部分幂等:重置 15 不删数据,按 marker 覆盖。

**Migration**:`0007_image_pipeline`

---

## P7 — Strapi Integration ✅

**任务与结果**

- Step 16(手动触发,非 pipeline 内)`strapi_sync`:创建/更新 Draft、图片上传(mainImage)、slug 冲突处理。
- **绝不自动 Publish**(spec §53)。
- `StrapiSyncRow` 同步状态持久化;Web [Push Draft / Update Existing Draft] 按钮。

**Migration**:`0008_strapi_sync`

---

## P8 — Web UI ✅

**任务与结果**

- 全部页面(Jinja2 + HTMX):新建文章、jobs 列表、job 详情(3s 轮询 fragment)、文章预览、keywords 页。
- API(`app/routes/jobs.py` 等):创建 job、查询、`GET /{id}/article|serp|sources|reviews`、Strapi 同步。
- 最小 cancel(`api_cancel_job`):非终态 job → `cancelled`(独立 session 提交)。
- 文章预览:渲染 markdown + 内部链接解析 + 图片静态路由。

**Migration**:无(复用 P0–P7 表)

---

## P9 — Reliability / Production Hardening ✅

### P9-A — 核心可靠性 ✅(已完成)

**任务与结果**

1. **按步重试**:`checkpoints.reset_from_step(N)` 只删 N..15 产物;`retry_step` 模式重跑尾部。
2. **断点续跑**:`resume_from_step` 不删数据,靠 per-source 幂等 + `first_incomplete_step` 跳步。
3. **强制源刷新**:`force_source_refresh` 绕过 SourcePage TTL 全部重抓并 upsert。
4. **Provider 超时**:全部超时来自 `Settings`(llm 300s / serp 120s / extractor 120s / image 180s / strapi 60s),统一重试策略。
5. **Tavily fallback(可选,spec §10.3/15)**:`TAVILY_API_KEY` 配置时 `build_extractor` → `FallingBackExtractor`(Exa 失败 URL 交 Tavily);无 key 时纯 Exa。
6. **Anti-copy 调优(spec §29)**:`app/services/anti_copy.py`(RapidFuzz,阈值 `anti_copy_min_overlap_words=12` / `anti_copy_min_similarity=0.85` 来自 Settings),`anti_copy_step` 在 reviser 中执行。
7. **运行中取消(spec §43.3)**:worker 每步边界 `session.refresh(job)` 读库,DB-truth;命中 cancelled 静默返回,尾部不覆盖为 READY。
8. retry/resume 互斥校验、`retry_step` 范围校验。

**Migration**:无(复用现有表;anti-copy 报告为内存态)

**测试**:`tests/unit/test_p9_reliability.py`(checkpoint 映射/reset 边界/force-refresh/retry-resume 互斥/Tavily 全套/fallback/build_extractor)、`tests/unit/test_anti_copy.py`、`tests/unit/test_llm_provider.py`(重试/超时/耗尽)、`tests/integration/test_p9_pipeline.py`(4 个:retry_step_14 保留检查点 / resume from 4 / 边界取消 / 缓存命中 vs 强制重抓)。全量 353 passed, 1 skipped(真实 Strapi,需 env flag)。

**已知遗留(留待 P9-B/C)**:cancel 不清空历史 `error_code`(与生产 cancel 路由行为一致,测试已按此断言)。

### P9-B — 可观测 ✅(B1/B2/B3 已完成)

拆分为 3 个子批次,每批交付中文报告后等用户确认再进入下一批:

- **P9-B1 — Cost tracking(spec §54)**:✅(已完成)。
  - Migration 0009:新表 `llm_usage`(`job_id, step, model, input_tokens, output_tokens, duration_ms` + 索引);`images`、`source_pages` 各加 `provider_cost NUMERIC NULL`(`serp_runs.provider_cost` 早已在 0003 中存在)。
  - LLM 计量:orchestrator 侧 `MeteredLLMProvider` 包装,每次逻辑调用(含 structured 的 ≤2 次修复)记 1 行 `llm_usage`(tokens + duration_ms);step 代码零改动,行随 step 自身 commit 落库,`reset_from_step` 删重跑步的旧行。
  - SERP cost 打通:dataforseo `credits` → `SERPResponse.provider_cost` → `SerpRun.provider_cost`。
  - 源 cost 打通:Exa `costDollars.total` → `ExtractedPage.provider_cost` → `source_pages.provider_cost`(仅 fresh 提取写,缓存命中不动)。
  - 图片 cost 打通:image provider `cost`/`usage.cost` 解析 → `GeneratedImage.provider_cost` → `images.provider_cost`。
  - 测试:单测 `test_p9b1_cost.py`(12 个)+ 集成 `test_p9_pipeline.py` 新增 cost-tracking 全链路用例。全量 366 passed, 1 skipped。
- **P9-B2 — Prompt version dashboard(spec §48)**:✅(已完成)。
  - Migration 0010:6 个记录 prompt 来源的结果表(`competitor_analyses`/`serp_syntheses`/`content_briefs`/`article_outlines`/`article_versions`/`article_reviews`)各加 `prompt_hash VARCHAR(64) NULL`;`images` 表补 3 列 `prompt_name/prompt_version/prompt_hash`(image_planner 的 provenance 原来只在日志里,本批起随计划行落库)。
  - 记录侧:全部 10 个 LLM 结果持久化点写入 `prompt_hash`(= `load_prompt` 的 SHA256,整文件含 front matter);`_article_common` 两个 helper 增加 `prompt_hash` 形参,writer/reviser/3 个 reviewer/image_plan 调用点透传。
  - 清单侧:`prompt_service.all_prompt_specs()` 扫描 `prompts/*.md`,12 个带 front matter 的版本化 prompt 入清单;2 个规则本(`seo_article_guideline`/`brand_visual_guideline`)无 front matter,单列为 rulebook(仅内容 hash)。
  - 聚合侧:`app/services/prompt_dashboard.py` 按 (name, version, hash) 三元组聚合各表的 result 行数 + 去重 job 数;`article_reviews` 按 `review_type` 区分三个 reviewer;`article_versions` 按 `prompt_name` 区分 writer/revisioner;三元组与当前文件不符的历史行进 `stale_versions`(hash 漂移提示)。
  - API `GET /api/prompts`(`app/routes/prompts.py`,与 spec 一致无 v1 前缀)+ Web 页 `/prompts`(nav 新增 "Prompts")。
  - 测试:单测 `tests/unit/test_p9b2_prompts.py`(5 个:12 prompt 清单/规则本排除/API 形状/聚合+stale 用例/页面渲染)+ 3 个集成表形状测试补 `prompt_hash` + 集成断言记录 hash == 磁盘文件 hash。全量 **371 passed, 1 skipped**。
- **P9-B3 — Better error UI**:✅(已完成)。**无 Migration**。
  - 中文映射:`app/services/error_catalog.py` 把全部 18 个 spec §52 `ErrorCode` 映射为 (category 中文原因 / reason 中文说明 / advice 处置建议);未知码走兜底 —— `HTTP_*`(provider 派生,按状态码解释)、`NO_PAGE`(部分抓取失败)、其余归"未知错误"并提示看 Logs。机器可读契约(`error_code`/`error_message`)不变,只是在其上加人类可读层。
  - 失败步骤定位:`last_failed_step` **读时派生**,不落库 —— `_error_payload` 调 `checkpoints.first_incomplete_step`(首个未提交检查点的步骤,1..15)+ `STEP_LABELS` 出人类标签。15 步全部完成时(如纯 Strapi 同步失败)为 `None`,面板引导去 Strapi 区重推。
  - 错误面板:job detail 的 HTMX 片段(`job_fragment.html`)原 `alert-error` 升级为 `.error-panel`:原因/说明/处置建议/原始信息四行 + 「从该步重试 · Step N — <标签>」按钮(隐藏字段 `mode=step&step=N`,直连 P9-A `retry_step` 端点,只重跑该步及后续步骤)。CSS 新增 `.error-panel` 系列。
  - 覆盖入口:REST `GET /api/jobs/{id}` 与 detail 页/fragment 共用同一 `error` 载荷(7 字段)。
  - 测试:单测 `tests/unit/test_p9b3_error_ui.py`(9 个:catalog 全覆盖 18 码/未知码兜底/派生步骤三档:无检查点→2、部分→9、全完成→None、非 failed 无 error/fragment 渲染含重试按钮与无步骤分支)。全量 **380 passed, 1 skipped**。

### P9-C — 运维 ✅(已完成)

**任务与结果** —— spec §58 P9 清单的 "Cleanup / Backup" 两词收口。用户已确认范围:**手动 CLI,默认 dry-run,带 round-trip 测试,无任何自动/后台删除,不加新 compose 服务。**

- **Cleanup**:`app/ops/cleanup.py`,`python3 -m app.ops.cleanup [--apply] [--days N] [--job-id ID]`。
  - 只删**终态且过保留期**的 job:自动路径限 `failed`/`cancelled`;保留窗口以 `completed_at` 为锚(所有失败路径与 cancel 都写该字段),`JOB_RETENTION_DAYS=0` = 永不删。
  - 成功 job(`ready`/`strapi_draft_created`)**永不自动删**;`--job-id` 可对任意终态 job(含成功)显式删除(操作员覆盖,不受保留期限制),非终态(在跑)job 一律拒绝。
  - 删除范围 = job 的全部 13 张从表行 + job 行本身(FK 安全顺序;`serp_results` 经 `serp_run_id ∈ serp_runs` 间接删);同步删本地图片目录 `{data_dir}/articles/{job_id}/`。
  - **`source_pages` 永不删**(跨 job 共享 TTL 缓存,过期即缓存未命中重抓);keywords/keyword_clusters 参考数据永不删。Strapi 同步失败的 job 停在非终态 `strapi_syncing` → 天然不在清理范围(状态机未动)。
  - 默认 **dry-run 零提交**,且用 `count_job_rows`(只读 SELECT)给出"将会删 N 行"的预览;`--apply` 才真正删。
- **Backup**:`app/ops/backup.py`,`python3 -m app.ops.backup create|list|restore <archive> [--clear] [--target URL]`。
  - `create` 产出一个时间戳 tar.gz(`seo_backup_*.tar.gz`,落 `BACKUP_DIR`,默认 `{data_dir}/backups`):`db/*.jsonl`(每表一行一 JSON,PK 列在前)+ `db/manifest.json`(app 标记/时间/各表行数)+ `artifacts.tar`(整个 `{data_dir}/articles` 图片树)。自动按 `BACKUP_KEEP`(默认 14)剪掉最旧的归档。
  - `restore` 把 db 段重导入目标库(默认 `DATABASE_URL`,或 `--target` 指定):按 **FK 父→子顺序**(`Base.metadata.sorted_tables`)、按 PK 先删后插 → **幂等**(重复 restore 不重复行);`--clear` 先按子→父清空所有表(注意:也会清掉 keywords 等参考表)。
  - 序列化 round-trip:UUID→hex、datetime→aware isoformat、JSONB/unicode 文本原样。
- **配置**:`app/core/config.py` + `.env.example` 新增 `JOB_RETENTION_DAYS=30` / `BACKUP_KEEP=14` / `BACKUP_DIR=`(空= `{data_dir}/backups`)。
- 两个工具都是纯手动、幂等、可单独在 worker/web 容器内跑;是否挂 cron 由运维方决定(本阶段不做)。

**Migration**:无(复用现有表;head 仍为 `0010_prompt_hash`)

**测试**:单测 `tests/unit/test_p9c_ops.py`(12 个:cleanup dry-run 零提交+行预览 / 成功·新失败·非终态·无 completed_at 全部跳过 / retention=0 全保留 / apply 全链删除但共享 source_page 存活+图片目录删 / --job-id 绕过保留期(成功 job 亦可)/ 非终态 --job-id 拒绝 / backup 归档布局+manifest / restore 跨库 round-trip(UUID/JSONB/unicode/FK 链)+幂等 / --clear 清杂行 / 同 PK 替换 / prune 保新删旧)。全量 **392 passed, 1 skipped**(真实 Strapi,需 env flag)。

---

## 关键工程约定(跨阶段速查)

- Python 3.10,命令用 `python3`(PATH 无 `python`);测试 `python3 -m pytest -q`。
- 15 步 pipeline;LLM 调用分布:step 1–3 零调用,step 4 五次(每来源一次),step 5–14 各一次,step 15 零调用。
- `_cancelled`(orchestrator)是 DB-truth,能看到 web 路由独立 session 的提交。
- `reset_from_step` 删除映射:≤2 删 SerpRun/SerpResult;≤3 删 JobSource;**SourcePage 从不删**(TTL 缓存跨 retry 存活);reset 15 不删(部分幂等)。
- 集成测试用真实 PostgreSQL;`check_database()` 失败时 skip。
- 交付节奏:每子批次交付中文报告(修改文件/实现说明/Migration/测试方法/测试结果/未解决问题),等用户确认再进入下一批。
