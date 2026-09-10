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
| 审计修复 B1–B7 | CODEX audit 全量收口(30 项) | ✅ | B1 `cf85665` · B2 `c50c235` · B3 `c6a4b83` · B4 `9462af0` · B5 `8af9212` · B6 `52d3643` · B7 HEAD |

> **口径诚实声明(L03)**:P0–P9 全部功能已实现;上述审计 B1–B7 把 CODEX 审计
> 报告(commit `c5c7101`,0 Critical / 12 High / 15 Medium / 3 Low,共 30 项)
> 全部修复并回归。自动化测试用 **fake providers + 本地 PostgreSQL** 验证逻辑
> 正确性(不调真实付费 API / 不写真实 Strapi)。**live 运行验收**(真实外部
> 服务跑通一整篇 READY 并人工抽查)是独立的上线前部署验收步骤,尚未完成。

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

## 审计修复批(CODEX audit 全量收口)✅(已完成)

**范围** —— 针对 CODEX 审计报告的全量修复,按优先级 H-1 → M-3 → M-1 → M-2 → L-1(H-2 仅回复说明,无代码改动)。

- **H-1 — READY 前统一 DoD 门禁**:✅(已完成)。**无 Migration**。
  - 新模块 `app/services/article_dod.py`:`validate_article_done(session, job) -> list[str]`(空列表 = 通过),纯只读,覆盖 spec §63 DoD 全部条款:SERP(run 存 raw_response、organic 结果、1–5 个来源且 `normalized_url` 唯一)、Research(竞品分析/综合/证据/简报/大纲 `valid=True`)、文章(标题/H1/seo_title/meta/slug/FAQ/CTA slot/`[[INTERNAL_LINK:*]]` 标记可解析/三审齐全/终版 anticopy 无严重重叠)、图片(hero 在 sort_order 0 且无 insertion marker、`hero.webp`/`inline-N.webp` 命名、alt_text、inline 标题存在于正文、1–3 张)。
  - **单一门禁点**:只挂在 `app/pipeline/orchestrator.py` —— `_run_steps` 成功、`_cancelled` 检查之后无条件执行,覆盖 fresh run / retry / resume / READY backfill 全部路径;不落在 `image_generate` 步骤内(步骤级直调单测/集成测用最小 fixture,天然豁免)。
  - 失败语义:抛 `PipelineError(ErrorCode.ARTICLE_VALIDATION_FAILED, "Definition of Done not met: " + 明细)` → job FAILED(`error_code`/`error_message` 落库),**检查点不动** → 可按步重试。
- **M-3 — Strapi author/category 端点可配置**:✅(已完成)。
  - `app/core/config.py` 新增 `strapi_author_plural_api_id`(默认 `authors`)/`strapi_category_plural_api_id`(默认 `categories`),对齐既有 `strapi_blog_plural_api_id` 模式;`strapi_cms.py` 的 `list_authors`/`list_categories` 改用配置端点;`.env.example` 补 `STRAPI_AUTHOR_PLURAL_API_ID` / `STRAPI_CATEGORY_PLURAL_API_ID`。
- **M-1 — `/settings` 页(spec §57)**:✅(已完成)。
  - `app/routes/web.py` 新增 `GET /settings`(nav 新增 "Settings" 链接,`app/templates/settings.html` + 4 个状态 badge CSS 类)。
  - 七行:LLM / DataForSEO / Exa / Image API / Strapi / PostgreSQL / Redis;每行只报四种状态词之一(§57):`Missing`(未配置)/ `Connected`(已配置且可达)/ `Failed`(已配置但不可达);`Configured` 保留在状态枚举内(已配置但未探测时使用,当前实现凡已配置必探测)。
  - 复用 `build_providers(settings)` + best-effort never-raise 模式(与 `app/routes/providers.py` 一致);DB 用 `check_database()`;Redis 用 `check_redis(settings.redis_url)` —— 该函数从 `app/main.py` 移到 `app/db/session.py`(避免 web→main 循环导入,`/health` 行为不变)。**不回显任何 secret(spec §60)**。
- **M-2 — 迁移并发说明(仅文档)**:✅(已完成)。
  - 事实:迁移是纯 Alembic、操作员手动 `alembic upgrade head`,web/worker 进程无任何 in-process DDL;单进程升级安全且幂等,需避免的是**两个并发** `alembic upgrade head`。README Quick start 段下新增 Migrations 注释框说明。
- **L-1 — README 措辞**:✅(已完成)。
  - "Current phase" 段改为"全部**实现**完成",并显式区分**实现口径**(全部功能落地 + `python3 -m pytest` 全绿)与**运行验收**(真实外部服务跑通一篇 READY 文章 + 人工抽查,属部署验收,与测试基线独立)。
  - 测试命令改为可复现的 `python3 -m pytest` 并注明基线数量。
- **H-2 — 审计方环境缺依赖(仅回复,无代码)**:✅(回复)。
  - 审计方环境无 `pytest`/`httpx`/`SQLAlchemy`/`openpyxl`,其"测试不可运行"结论是环境性假象;本环境全量绿,见下。

**测试**

- 新增 `tests/unit/test_article_dod.py`(20 个:合规 job 通过 + 只读性 / 6 个文章字段缺失参数化 / 缺 style 审 / 缺终版 anticopy / 严重重叠 / 未知 INTERNAL_LINK 标记 / SERP run 缺失(来源留存)/ 重复 normalized_url / 6 个来源 / 大纲 invalid / 删光 Research 四行 / 坏图片计划 4 错 / 无图片 / 无版本)。
- 新增 `tests/integration/test_p8_pipeline.py::test_dod_gate_fails_ready_without_faq`(e2e:reviser 稿缺 FAQ → `run_job_pipeline` → job FAILED + `ARTICLE_VALIDATION_FAILED` + "Definition of Done not met",且 SerpRun/文章版本/hero 图检查点全部留存)。
- 新增 `tests/unit/test_strapi_cms_provider.py::test_author_and_category_api_ids_are_configurable`(自定义 `writers`/`tags` 端点被命中,默认端点不被访问)。
- 新增 `tests/unit/test_p8_routes.py::test_web_settings_page_statuses_only`(七行齐全、仅四种状态词、4 Missing + 2 Connected + 1 Failed 断言,无 secret 回显)。
- 配套 fixture 修正:p8 `REVISER_DRAFT` 补 FAQ 段(DoD 合规);p9 fixture 早已合规。
- 全量:**415 passed, 1 skipped**(基线 392 + 新增 23)。

---

## 审计修复批 B1–B7(CODEX audit 全量收口,30 项)✅(已完成)

> 依据 `CODEX-AUDIT-REPORT.md`(审计 commit `c5c7101`;结论:整体验收不通过,
> 0 Critical / 12 High / 15 Medium / 3 Low,共 30 项)。按 AGENTS.md 的 P0→P9
> 阶段约束与报告 §11 建议分批;每批次:修复 → 测试 → 完整回归 → 更新文档 →
> 单 commit。测试口径不变:**fake providers + 本地 PostgreSQL**,不调真实付费
> API / 不写真实 Strapi。逐项明细见 `docs/AUDIT-FIX-PROGRESS.md`。
>
> 测试基线演进:审计前 `415 passed, 1 skipped` → B1 `426` → B2 `448` → B3 `453`
> → B4 `478` → B5 `486` → B6 `501` → **B7 `520 passed, 1 skipped`**。
> 新增迁移仅 B1 的 `0011_error_raw_usage_prompt`;B2–B7 均无 schema 变更。

| 批次 | 阶段 | 修复项(H=High, M=Med, L=Low) | commit | 批次测试 |
|---|---|---|---|---|
| B1 | P0 / P1 | M04, H08, L02, M09, M11 | `cf85665` | 426 passed |
| B2 | P2 | H01, H02, M15 | `c50c235` | 448 passed |
| B3 | P3 | H04, M05 | `c6a4b83` | 453 passed |
| B4 | P4 / P5 | H09, H06, H07, H11, M08 | `9462af0` | 478 passed |
| B5 | P6 | H05, H10, M06, M07, L01 | `8af9212` | 486 passed |
| B6 | P7 | H03, M01, M02 | `52d3643` | 501 passed |
| B7 | P8 / P9 + 文档 | M13, M14, M12, M10, M03, H12, L03 | HEAD | 520 passed |

### B1 — P0 / P1(commit `cf85665`)

- **M04** Dockerfile 补拷 `alembic.ini`(容器内可执行迁移)。
- **H08** RQ 长 pipeline 超时:新增 `rq_job_timeout_seconds=3600`,`enqueue_*` 显式传 timeout。
- **L02** schema 去重:删除 `serp.py`/`sources.py`/`images.py` 重复 `provider_cost`。
- **M09** LLM 计量溯源:`llm_usage` 增 `prompt_name/version/hash`(迁移 0011)+ `set_llm_prompt()` helper 贯通 13 处 LLM 调用;`model` 不再恒 None。
- **M11** 失败 raw 持久化:`generation_jobs.error_raw`(迁移 0011),orchestrator 两分支写 raw/traceback,`/api/jobs/{id}` 暴露,retry 清空。
- 迁移:`0011_error_raw_usage_prompt`。

### B2 — P2(commit `c50c235`)

- **H01** DataForSEO 解析按官方 Live Advanced 契约重写(`result`=block 列表、`items[]` 扁平混合按 `type` 分派、rank 取 `rank_group`、PAA 嵌套、related 字符串)+ 新增 **featured snippet**。`test_dataforseo_provider.py` 整文件重写(23 用例)。
- **H02** Exa `extract()` 显式 `text: True`(API 默认 false → 否则无正文)。
- **M15** 三家 provider `health_check()` 严格化,消除 401/403 假阳性(DataForSEO 付费 SERP + `status_code==20000`;Exa 零成本"非法 body 认证探测";Tavily 付费探测 + 形状校验)。
- **M12 前置子项**:`_extract_cost()` 改读官方 `cost`(USD),弃用 `credits`(主体在 B7)。

### B3 — P3(commit `c6a4b83`)

- **H04** `prepare_keyword` 改 `async def`(orchestrator 统一 await;旧 sync 对已知词必 TypeError)。新增 orchestrator 级已知词全链路回归。
- **M05** 导入每 sheet 先 `casefold` 内存去重(消除 `autoflush=False` 下重复新词 IntegrityError);全部关键词比较改 `lower()` 字面量精确(`%`/`_` 不当 LIKE 通配符)。

### B4 — P4 / P5(commit `9462af0`)

- **H09** 新增 `evidence_verification.py`——证据来源 URL 由独立提取器通道核实,不可达/空正文 → `usage="avoid"` + `confidence="low"` + `source_unverified:` 标记。
- **H06** DoD gate 重写为 Markdown 结构解析完整性门禁(Setext H1、FAQ 逐题、CTA 正文、图片文件存在性、marker 大小写不敏感、研究/审核/竞品分析关联完整性)。
- **H07** 新增 `final_body.py::render_final_body`——内链→图片两级解析 + 残留 marker 响亮失败;本地导出与 Strapi 同步两条终稿路径统一走共享渲染器。
- **H11** 文章 versions/reviews 改**不可变 append-only 历史**——reset/retry 不删,当前草稿由 `latest_writer_version()` 派生;DoD 新增"最新版本必须为 revision 阶段"门禁。
- **M08** `job.strategy`(六种策略语义)贯通进 Brief prompt;writer/reviser/image_plan 温度改读 Settings。

### B5 — P6(commit `8af9212`)

- **H05** `ready` 状态归属改到 orchestrator(`validate_article_done` 通过后同事务置 `ready`);`POST /jobs/{id}/sync-strapi` 加服务端状态门禁(非 syncable → 409)。
- **H10** `OpenAIImageProvider._download()` 去 `Authorization` 头,Images API key 不随响应 URL(第三方 CDN / 预签名)外泄。
- **M06** image step 重跑复用已通过 M07 校验的 `local_path`,仅重做缺失/损坏/格式不符的行。
- **M07** 新增 `image_storage.py` 图像契约层:`.webp` 文件名强制真 WebP(Pillow 转码)+ 魔数/解码/扩展名一致性;测试 fixture 换真实可解码 PNG。**新增依赖 `pillow>=10.0`**。
- **L01** 新增 `job_artifacts.py::export_research_artifacts`,§34 目录补全 `content-brief/outline/serp/review/sources.json`。

### B6 — P7(commit `52d3643`)

- **H03** Strapi 5 REST 契约解析:entry 扁平 `data`(无 `attributes` 包裹)、`/api/upload` `data` **数组**取 `[0]`、`get_draft` 带 `populate[...]`;v4 形状保留为兜底。新增 `relation_document_id()/media_url()/media_id()`。
- **M01** 新增非 terminal 状态 `strapi_sync_failed`(§64)——pre-sync / 中途失败 / 缺本地图片三条路径落统一持久化状态(保留 `strapi_document_id` 幂等锚);同步重试走 sync 专用通道,自动清理永不扫;UI 红色 `badge-error` + "Sync failed" 告警区。
- **M02** STEP F GET verify 升级为**逐字段值比对**(id/documentId/draft 状态/title/slug/终稿 body/meta/规范化 keywords/relations/mainImage URL 路径),问题合并为单条 `STRAPI_SCHEMA_MISMATCH`。

### B7 — P8 / P9 + 文档(commit HEAD)

- **M13** Job Detail 补研究正文 + Logs 区:`competitor_analyses`(逐条 + 源页 title/url/analysis/model)、`serp_synthesis`(最新 synthesis)、`evidence_notes`(claim/source/type/confidence/usage/note)、`logs`(current_step + job error + 全 15 步 `checkpoint_status`);`job_fragment.html` 新增 4 个卡片,全部**读时派生、无 schema 变更**。
- **M14** enqueue 失败降级 + 安全重投:入队失败 job 保持 `queued` 并写 `error_code="ENQUEUE_FAILED"`(非 terminal,符合 §43.3 降级到手动重试);`CreateJobResponse` 增 `enqueued/error`;新增 `POST /jobs/{id}/enqueue`(仅 `status==queued` 可重投,拒绝 terminal 与非 queued,成功后清 error 标记)。
- **M12** 成本账本(`llm_usage`)改**不可变 append-only 台账**(§54):`reset_from_step` 不再删 usage 行——重试只追加新行、累加成本,不删历史;`source_extract` 缓存命中零新增成本(不重刷 `provider_cost`)、`image_generate` 复用零新增;`_extract_cost()` 读 `cost` 非 `credits`;None 成本保持 None(不造 0)。
- **M10** JSON 序列化 secret 脱敏:`redact()/redact_value()/redact_dict()`(Bearer → KV → URL-query 顺序);`JsonLogHandler`、orchestrator 异常路径、strapi_sync 失败路径全部脱敏(`error_message`/`error_raw`/traceback),secret 不进日志/web(§60)。
- **M03** 预览安全渲染 + 图片 placement:`MarkdownIt("default", linkify=False)`(转义原始 HTML / 属性值,渲染 GFM 表格,不发 `javascript:` 链接 → 输出可安全 `|safe`);`_resolve_preview_body()` 解析内链 → hero 图(role==hero)→ inline 图(仅本地文件存在时插入)→ 渲染;preview 路由与 `article_preview.html` 传 `alt`。
- **H12** 备份含图片但 restore 不恢复 → `restore_archive` 解包 `artifacts.tar`,**写库前校验图片文件**(缺失 → `BackupError`,DB 不动),`_remap_local_path` 重锚 `articles/`,`validate_archive`(有 image 行但无 artifacts → 报错);CLI `restore --data-dir`。
- **L03** 诚实文档:`README.md` 现状段改为 B1–B7 逐批 commit 表 + mock/live 验收边界声明(测试全绿 = 逻辑正确性;live 运行验收为独立上线前步骤,尚未完成);`docs/PHASE-LOG.md` 总览表增审计行、新增本节、测试基线更新至 `520 passed`。

**B7 测试结果**:`520 passed, 1 skipped`(B6 后 501 + 净增 19:M03 ×3、M13 ×1、M14 ×2、M12 ×4(3 新 + 2 改写)、M10 ×9、H12 ×5 等,含既有断言按新语义改写)。**无新迁移**。

---

## 关键工程约定(跨阶段速查)

- Python 3.10,命令用 `python3`(PATH 无 `python`);测试 `python3 -m pytest -q`。
- 15 步 pipeline;LLM 调用分布:step 1–3 零调用,step 4 五次(每来源一次),step 5–14 各一次,step 15 零调用。
- `_cancelled`(orchestrator)是 DB-truth,能看到 web 路由独立 session 的提交。
- `reset_from_step` 删除映射:≤2 删 SerpRun/SerpResult;≤3 删 JobSource;**SourcePage 从不删**(TTL 缓存跨 retry 存活);reset 15 不删(部分幂等)。
- 集成测试用真实 PostgreSQL;`check_database()` 失败时 skip。
- DoD 门禁(§63)只有一个点:orchestrator `_run_steps` 成功后无条件执行
  `validate_article_done`(fresh/retry/resume/backfill 全覆盖);步骤级直调(如
  `run_image_generation`)天然豁免。门禁失败 = FAILED +
  `ARTICLE_VALIDATION_FAILED`,检查点不动、可按步重试。
- 交付节奏:每子批次交付中文报告(修改文件/实现说明/Migration/测试方法/测试结果/未解决问题),等用户确认再进入下一批。
