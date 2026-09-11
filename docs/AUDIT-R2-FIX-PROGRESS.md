# 审计修复进度跟踪（第二轮 CODEX Audit → 分批修复）

> 依据：`CODEX-AUDIT-REPORT.md`（审计结论：本轮离线验收仍不通过；
> 0 Critical / **7 High** / **3 Medium** / **1 Low**，编号 `R-H01`…`R-L01`）。
>
> 本轮编号与第一轮（`docs/AUDIT-FIX-PROGRESS.md` 的 H01…L03，30 项）相互独立；
> 报告 §6 给出逐项映射，避免把已修复问题继续当作当前缺陷。
>
> 修复顺序遵循 `AGENTS.md` 的 P0 → P9 阶段约束与报告 §8 建议：
>
> ```
> B8  P0      R-H01                       RQ job_timeout
> B9  P1/P2   R-H04                       Provider raw / 脱敏合同
> B10 P4      R-H06                       Evidence 支持性核实
> B11 P5      R-H05 + R-M03               DoD 门禁 + 审核血缘
> B12 P7      R-H02 + R-H03               Strapi 上传数组 + 重试幂等
> B13 P9      R-H07 + R-M01 + R-M02       恢复路径安全 + 恢复原子性 + 付费成本账本
> B14 文档    R-L01                       进度文档诚实口径
> ```
>
> 每批次完成后：更新本文件 → 运行完整测试套件 → 在 `docs/audit-r2/` 输出该阶段详档。
> 内容规则以 `SEO-AUTO-DEV-SPEC.md` 为最高优先级；不调用真实付费 API、不写真实 Strapi，
> 全部使用 mock / fixture / 内存 SQLite 验证（与审计授权范围一致）。

## 基线

| 项 | 值 |
|---|---|
| 审计轮次 | 第二轮（报告基于 commit `0636e2e5410dfc6ed79f9d536a9a1637761a37fe`） |
| 本轮起点 HEAD | `0636e2e` |
| 基线测试 | `520 passed, 1 skipped, 1 warning`（`.venv/bin/python -m pytest -q -p no:cacheprovider`） |
| 环境 | Python 3.10.12 / RQ 2.12.0 / SQLAlchemy 2.x |
| 豁免项（报告 §1） | 真实 PostgreSQL/Redis/LLM/DataForSEO/Exa/Image/Strapi 调用与 Strapi Admin 人工验收 |

## 批次总览

| 批次 | 阶段 | 修复项 | 状态 |
|---|---|---|---|
| B8 | P0 | R-H01 | ✅ 完成 |
| B9 | P1 / P2 | R-H04 | ✅ 完成 |
| B10 | P4 | R-H06 | ✅ 完成 |
| B11 | P5 | R-H05, R-M03 | ✅ 完成 |
| B12 | P7 | R-H02, R-H03 | ✅ 完成 |
| B13 | P9 | R-H07, R-M01, R-M02 | ✅ 完成 |
| B14 | 文档 | R-L01 | ✅ 完成 |

## 最终状态

| 项 | 值 |
|---|---|
| 本轮修复 commit | `4de7ab4`（`git show --stat 4de7ab4`） |
| 前轮基线 commit | `0636e2e`（第二轮审计所基于的 HEAD） |
| 全量测试 | `587 passed, 1 skipped, 1 warning`（无失败；跳过项为 `RUN_EXTERNAL_INTEGRATION_TESTS` 门控的真实 Strapi §59.3 流程） |
| 新增迁移 | `0012_evidence_support_check`、`0013_review_lineage`、`0014_provider_cost_ledger`（均已 upgrade 本地 PG 测试库；`alembic upgrade head --sql` 离线生成成功；单 head） |
| 新增依赖 | 无（复用已有 `markdown-it-py`） |
| 未证明项 | 真实 PostgreSQL 迁移/集成、真实 Redis Worker、真实 Provider 全链路至 READY、专用 Strapi Draft §64 人工检查（报告 §1 授权豁免，属上线前部署验收） |

### 审计 11 项逐项收口状态

| 编号 | 级别 | 批次 | 状态 | 回归范围 |
|---|---|---|---|---|
| R-H01 | High | B8 | ✅ 已验证 | 真实 RQ `Queue.parse_args` 参数解析 + 任务签名绑定（2 个 enqueue helper） |
| R-H02 | High | B12 | ✅ 已验证 | 标准 Strapi 201 顶层数组解析 + 真实 Provider 全 A–F sync + 非 PipelineError 失败态 |
| R-H03 | High | B12 | ✅ 已验证 | 前置失败→补配置→重试复用行；create HTTP 失败→重试复用行 |
| R-H04 | High | B9 | ✅ 已验证 | 真实 DataForSEO/Exa 错误 → 编排器失败分支 → DB `FAILED` + 安全 raw；dict raw 纵深防御 |
| R-H05 | High | B11 | ✅ 原复现已验证；R3 有后续边界 | Markdown H1/FAQ/当前 writer 已修；HTML H1 与 review retry→resume 由 R3-M01/R3-H01 后续修复 |
| R-H06 | High | B10 | ✅ 原复现已验证；R3 有后续边界 | 不相关/固定反驳/缺数字/题名不符已修；方向相反、数字子串、同作者不同文献由 R3-H02 后续修复 |
| R-H07 | High | B13 | ✅ 已验证 | 6 种恶意 member + symlink → 零写入、零 DB 变动 |
| R-M01 | Med | B13 | ✅ 已验证 | DB 失败后 live 文件恢复 ORIGINAL；成功路径仍覆盖 |
| R-M02 | Med | B13 | ✅ 原复现已验证；R3 有后续边界 | 成功调用的成本历史已修；已报告费用但业务结果失败由 R3-M02 后续修复 |
| R-M03 | Med | B11 | ✅ 已验证 | 审核 attempt append-only + revision `based_on_reviews` 血缘 |
| R-L01 | Low | B14 | ✅ 已验证 | README + 两份进度文档诚实更正（9 项改判 + 边界声明 + 本轮 commit） |

> 说明：R-H01…R-M03 的"已验证"指本轮离线范围（SQLite/内存 + 本地 PostgreSQL +
> MockTransport + 真实已安装依赖）内的代码与测试证据；真实服务运行验收仍未执行，
> 也不作为本轮放行条件（报告 §1 / §8）。

### 第三轮复验后的更正（2026-09-11）

R2 表中的“完成”只表示其列出的**原始复现**在当时环境通过，不再解释为所有相邻
边界均已证明。第三轮报告在 HEAD `92a919e` 上发现并由当前工作树修复：

| R3 编号 | 当前处理 | 本机证据 |
|---|---|---|
| R3-H01 | 审核/修订增加显式失效标记；Resume 与 DoD 校验当前 review lineage | retry Fact→Style 未完成时恢复点为 step 12；过期 revision 被 DoD 拒绝 |
| R3-H02 | 数值单位 token、句级有序 claim、方向冲突、强化题名身份匹配 | `97%≠1970`、reduces≠increases、newsletter≠trial 均拒绝 |
| R3-M01 | Markdown parser 的 `html_block/html_inline` 另行拒绝 HTML H1 | 大小写、属性、跨行均拒绝；fenced/inline code 字面量不误报 |
| R3-M02 | DataForSEO 失败异常携带已观察费用；流水独立于失败回滚提交 | 空 organic + cost=0.05 留一条 0.05 charged；未报金额留 NULL；网络失败不猜测收费 |
| R3-L01 | 本节、README 与阶段文档修正数量及验证范围 | 第二轮总数明确为 11；历史测试数与本轮结果分离 |

当前本机结果：`556 passed, 48 skipped, 1 warning`。48 项为 PostgreSQL/真实外部服务
条件跳过；R2 记录的 `587 passed, 1 skipped` 保留为其原环境历史结果。

## 明细跟踪表

状态图例：✅ 已修复（含测试证据）｜🔧 进行中｜⏳ 待办

### B8 — P0 ✅ · R-H01

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| R-H01 | High | `Queue.enqueue(..., timeout=3600)` 不是 RQ 控制参数。RQ 2.12 `Queue.parse_args` 只提取 `job_timeout`；`timeout` 变成任务 kwargs，`process_job(job_id, options)` / `sync_strapi_draft(job_id)` 均在进入函数体前报 `unexpected keyword argument 'timeout'`，业务失败落库逻辑不执行，网页可能一直 `queued` | 两个 enqueue helper 的 `timeout=` 改为 RQ 真正的控制参数 `job_timeout=`（两处）；`Settings.rq_job_timeout_seconds` 注释同步说明参数名 | `app/workers/article_tasks.py`、`app/core/config.py` | `tests/unit/test_h08_rq_timeout.py` **整文件重写**（5 用例）：不再用 fake queue 自证 kwargs，而是把 helper 交给 `queue.enqueue` 的真实实参喂进**已安装 RQ 的 `Queue.parse_args`**，断言（a）`parsed.timeout == 4321`、（b）任务 kwargs 不含 `timeout`、（c）`inspect.signature(真实任务).bind()` 成功；另含一条防漂移用例证明 `timeout=` 仍会被 RQ 当作任务 kwarg 并导致绑定失败 | ✅ |

**B8 测试结果**：`tests/unit/test_h08_rq_timeout.py` → `5 passed`；全量待 B14 汇总复跑。
**无新迁移**（无 schema 变更）。

### B9 — P1 / P2 ✅ · R-H04

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| R-H04 | High | 多个 Provider 把 **dict** 作为 `PipelineError.raw` 传入（dataclass 不做运行时类型校验），而编排器失败分支调用只接受字符串的 `redact(error.raw)` → 在 `except PipelineError` 内部再抛 `TypeError`，发生在 commit 之前；业务失败（原错误码与 raw）全部丢失，job 停留先前状态 | ① `app/core/redaction.py` 新增 **`redact_raw(value, max_chars=20000)`**：统一 raw 类型边界——`None`→`None`、`str`→`redact`、结构化 payload→`redact_value` 递归脱敏后 `json.dumps`（不可序列化回退 `str()`），最终**恒为脱敏字符串**并可截断；② `PipelineError.__post_init__` 调用 `redact_raw`，在**构造边界**保证 `raw` 真的满足注解 `str | None`（所有现有/未来 Provider 自动受保护）；③ 编排器 `PipelineError` 分支改用 `redact_raw(error.raw)`（纵深防御：即使有代码绕过构造函数写入 dict 也不会崩）；④ `source_extract` 记录最后一次每-URL 抽取失败的 raw，全部来源失败时让 `SOURCE_EMPTY` 携带该（已脱敏）诊断 raw，而不是丢弃 Provider 细节；⑤ DataForSEO / Exa 边界补注释说明"允许传结构化 payload，由 PipelineError 统一归一化" | `app/core/redaction.py`、`app/core/exceptions.py`、`app/pipeline/orchestrator.py`、`app/pipeline/steps/source_extract.py`、`app/providers/serp/dataforseo.py`、`app/providers/extractor/exa.py` | ① `tests/unit/test_r2_redaction_raw.py`（9 用例）：`redact_raw` 的 None/str/dict/嵌套 secret/list/不可序列化回退/截断；`PipelineError` 构造 dict raw → 字符串且可 `json.loads`、secret 消失；str raw 不变形；默认 None。② `tests/integration/test_p9_pipeline.py` 新增 4 个**真实 Provider 错误 → 编排器失败分支 → DB** 回归：DataForSEO 业务鉴权失败（真实 `parse`）、DataForSEO 空 SERP（真实 `search` + MockTransport）、Exa 空正文（真实 `extract` + MockTransport）、以及"绕过构造函数强写 dict raw"的纵深防御用例；断言 job `FAILED` + 原 `error_code` + `error_raw` 为**合法 JSON 字符串**且已脱敏 | ✅ |

**B9 测试结果**：全量 `534 passed, 1 skipped, 1 warning`（B8 后 521 + 新增 13：redaction/PipelineError 单元 9、编排器 raw 回归 4）。
**无新迁移**（`generation_jobs.error_raw` 为 TEXT，无需变更）。
**关键设计决策**：raw 归一化放在 `PipelineError.__post_init__`（而非逐个 Provider），确保"统一类型"这一合同对**所有** Provider 生效，且编排器再加一层 `redact_raw` 纵深防御。

### B10 — P4 ✅ · R-H06

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| R-H06 | High | Evidence verifier 只证明"网页有文字"：只要提取器返回任意非空正文，就保留模型给出的 `source_title/claim/confidence/usage`。真实网址配捏造论文/数字（如来源仅 "Welcome to our homepage."）仍判 `high / supported`；也不保存支持 claim 的证据片段 | 把"可达性检查"升级为**支撑性核实**（纯函数 `assess_source_support`），按 4 条规则比对抓取正文：① **来源题名印证**（`source_title` 的非通用词/年份须出现在页面上，排除 study/research/journal 等通用词）；② **数字印证**（claim 中每个统计数字必须出现在正文，兼容 `97 %`/`97 percent`/`0.97` 拼写）；③ **主题词重合度**（claim 显著词在正文占比 ≥ 0.25，否则视为"可达但不相关"）；④ **反驳检测**（claim 相关句中含 `no evidence`/`debunked`/`not supported` 等否定标记）。判定结果映射：`unsupported`→`confidence=low` + `usage=soften`，`contradicted`/`unverified`→`low` + `avoid`；**所有判定**保存 `verification_status` 与 `supporting_excerpt`（印证/反驳片段）。新字段随 Writer/Fact-Reviewer 上下文、Job Detail、§34 导出一起暴露 | `app/services/evidence_verification.py`（重写）、`app/schemas/research.py`、`app/db/models/research.py`、`app/pipeline/steps/evidence_research.py`、`app/pipeline/steps/_article_common.py`、`app/routes/jobs.py`、`app/services/job_artifacts.py`、**新增迁移 `0012_evidence_support_check`** | ① `tests/unit/test_r2_evidence_support.py`（12 用例）：支持/可达但不相关/正文反驳/缺数字/percent 拼写/decimal 拼写/论文题名不符/空正文，以及 `verify_evidence_sources` 的 soften/avoid/支持/不可达四态与片段保存。② `tests/unit/test_research_steps.py`：原"任意非空正文即通过"的正向 fixture 改为**真实支持性正文**（并断言 `verification_status`/`supporting_excerpt`），新增"可达但不相关→soften"与"正文反驳→avoid"步骤级用例 ×2。③ `tests/integration/test_generation_job.py` 新增迁移列断言。④ 全量 `549 passed` | ✅ |

**B10 测试结果**：全量 `549 passed, 1 skipped, 1 warning`（B9 后 534 + 新增 15）。
**新增迁移**：`0012_evidence_support_check`（`evidence_notes.verification_status` VARCHAR(16) NULL、`evidence_notes.supporting_excerpt` TEXT NULL）；已 upgrade 本地 PG 测试库，离线 SQL 生成成功。
**关键设计决策**：判定器为**确定性纯函数**（不引入 V2 检索/额外 LLM），符合审计"允许独立判定器，不要求引入 V2 检索"；`supported` 的 `note` 保持原样（不污染既有语义），判定另存新列。

### B11 — P5 ✅ · R-H05 + R-M03

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| R-H05 | High | DoD 仍放行三类内容缺陷：① `_faq_question_count` 只数 `###`，三个**空标题**可过 gate（测试还明确接受该行为）；② H1 正则漏掉 CommonMark 允许的 1–3 空格缩进 ATX H1 与单 `=` Setext H1（`X\n=\n`），而项目渲染器把二者都渲染为 `<h1>`；③ `job_reviews` 按整个 job 聚合，新 writer v3/revision v4 没有本轮 seo/fact/style，只要旧稿有 review + v4 有 anticopy 即可通过 | ① **Markdown H1 与 FAQ 改由 token 解析**：缩进 ATX / 单 `=` Setext 可捕获，代码块内 `#` 不误判；R2 此处**没有覆盖原始 HTML `<h1>`**，该边界由 R3-M01 另行修复。② **审核血缘**：三份 review 必须挂在 `latest_writer_version()`；同 writer 新 attempt 后旧 revision 的边界由 R3-H01 另行修复。③ anti-copy 校验取 final 版本最高 attempt | `app/services/article_dod.py`、`tests/unit/test_article_dod.py`、`tests/unit/test_article_steps.py` | R2 原始 Markdown/FAQ/旧 writer 复现通过；HTML H1 与 retry→resume 组合不计入 R2 当时证据，见上方第三轮更正 | ✅ 原复现；R3 补边界 |
| R-M03 | Med | `persist_review` 对相同 (writer/version/type) **先 delete 再 insert**：重跑 Fact Review 后原 review ID 消失，已保留的旧 revision 无法追溯其输入（reset 不删 review ≠ 写入链 append-only） | 审核改为**append-only 运行历史**：① `article_reviews.attempt`（INTEGER，默认 1）记录同一 (version,type) 的第 N 次运行，"当前有效"由 `latest_review_row()`（max attempt）**派生**而非删除；② `persist_review` 不再 delete，改为 `attempt = max+1` 追加；③ `article_versions.based_on_reviews`（JSONB）由 reviser 通过 `review_lineage()` 写入本次 revision 实际消费的 `{type: {review_id, attempt}}`，使每个 revision 都能定位其审核集合 | `app/db/models/article.py`、`app/pipeline/steps/_article_common.py`、`app/pipeline/steps/article_reviser.py`、**新增迁移 `0013_review_lineage`**、`tests/unit/test_article_steps.py`、`tests/integration/test_p9_pipeline.py` | `tests/unit/test_article_dod.py`：`test_r_m03_review_retry_appends_and_keeps_history`（两次 fact review → attempt [1,2]、原 ID 仍在、`latest_review` 取 REPLACEMENT、`review_lineage` attempt=2）、`test_r_m03_latest_anticopy_attempt_wins`（新 attempt 覆盖旧判定 + 反向）；`tests/unit/test_article_steps.py` 旧用例 `..._replaces_same_version_type` 改写为 `..._appends_attempt_and_keeps_history`；`tests/integration/test_p9_pipeline.py` 全链路断言 revision 的 `based_on_reviews` 含 seo/fact/style 且 review_id/attempt 有效 | ✅ |

**B11 测试结果**：全量 `559 passed, 1 skipped, 1 warning`（B10 后 549 + 新增 10）。
**新增迁移**：`0013_review_lineage`（`article_reviews.attempt` INTEGER NOT NULL DEFAULT 1、`article_versions.based_on_reviews` JSONB NULL）；已 upgrade 本地 PG 测试库，离线 SQL 生成成功。
**关键设计决策**：① H1/FAQ 判定从**正则**迁移到 **Markdown token**（审计明确要求"以 Markdown token 解析判定标题和问答"），与渲染器共用解析器，消除语法覆盖差异；② "当前有效 review"用 `attempt` **派生**，不删除历史；revision 通过 `based_on_reviews` 固化其输入集合。

### B12 — P7 ✅ · R-H02 + R-H03

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| R-H02 | High | ① `_upload_result` 从 `data.get('data')` 起始解析，只把 `{data:[...]}` 当数组；而标准 Strapi upload 响应本身就是**顶层数组** `[{id,url,...}]`（HTTP 201），调用直接 `AttributeError: 'list' object has no attribute 'get'`；② 非 `PipelineError` 异常绕过失败状态处理：sync step 无统一兜底分支，Worker 只记 crash，job 可能遗留 `strapi_syncing`、sync row 停留 `in_progress` | ① `_upload_result` 接受**顶层数组**（标准）、`{data:[...]}`（代理包裹）、`{data:{...}}`/裸对象（v4）；空/畸形一律转稳定 `PipelineError`（新增 `_safe_json` 容错序列化，错误构造本身不再可能崩）；② `run_strapi_sync` 新增 `except Exception` 兜底：记 `logger.exception` → `session.rollback()`（不继续使用失败事务）→ 统一持久化 `strapi_sync_failed`（job error_code=`UNEXPECTED` + `error_raw` 完整 traceback 脱敏；sync row FAILED 且**保留 documentId**）；失败持久化抽成 `_persist_sync_failure()`，PipelineError 与未知异常共用同一分支 | `app/providers/cms/strapi_cms.py`、`app/pipeline/steps/strapi_sync.py` | `tests/unit/test_strapi_cms_provider.py`：`_upload_response` 默认改为**顶层数组**、新增标准数组用例 ×1、包裹数组用例 ×1、空/畸形参数化 ×6；`tests/unit/test_strapi_sync_step.py`：真实 `StrapiCMSProvider` + MockTransport（标准 201 顶层数组、扁平 entry、populated relations）跑完整 A–F sync 用例 ×1、非 PipelineError → `strapi_sync_failed`/`UNEXPECTED`/保留 docId 用例 ×1 | ✅ |
| R-H03 | High | 已有失败 sync 行但 `documentId` 为空时（首次缺 author/category 前置失败或 create HTTP 失败都会留下这种行），重试条件 `row is None or existing_doc_id is None` 成立 → 远端创建 Draft 后又**新建同 job_id 的行**，违反 `UNIQUE(strapi_syncs.job_id)`；rollback 后原行 documentId 仍为空 | 区分"**无行**"与"**有行但无 ID**"：`row is None` 才新建行；`row is not None and existing_doc_id is None` 时把新建 Draft 的 `strapi_id`/`strapi_document_id` **写回已有行**；新建行的 flush 加 `IntegrityError` 兜底（rollback → 重查锚点 → 复用行），绝不在失败事务上继续操作 | `app/pipeline/steps/strapi_sync.py` | `tests/unit/test_strapi_sync_step.py`：**审计原始复现**（缺 author 前置失败 → 补配置 → 重试成功；断言仍只有 1 行、行 ID 不变、documentId 写回）×1；create HTTP 失败 → 重试成功（仍 1 行）×1 | ✅ |

**B12 测试结果**：全量 `571 passed, 1 skipped, 1 warning`（B11 后 559 + 新增 12）。
**无新迁移**（`strapi_syncs` / `generation_jobs` schema 未变）。
**关键设计决策**：失败持久化收敛到 `_persist_sync_failure()` 单入口，PipelineError 与未知异常语义一致；标准上传响应以**顶层数组**为准，`{data:[...]}` 仅作兼容分支（审计 §4.2 明确"Blog 扁平化修复不能代替 Upload 合同修复"）。

### B13 — P9 ✅ · R-H07 + R-M01 + R-M02

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| R-H07 | High | 恢复归档无路径规范化：仅检查 arcname 以 `articles/` 开头，未拒绝 `..`／绝对路径／symlink escape，也未校验最终 resolved 归属；`articles/../escaped.txt` 通过 validate 并写到 staging 父目录，更深父路径可越过 data_dir 覆盖任意文件 | ① 新增 `_artifact_rel_parts()`：在**任何写入前**把每个 inner-tar member 规范化为 `articles/` 下的安全相对路径——拒绝绝对路径（含 `/`、`C:`、`\\`）、`..`/`.`/空组件、含 `:` 组件、非 `articles` 根、NUL；② `validate_archive` 对**每个** member 执行该检查并拒绝 symlink/hardlink member（零写入即失败）；③ `_assert_within()` 在 materialize 时再做 resolved containment（纵深防御）；④ `_iter_artifact_files` / `_stage_artifacts` 复用同一检查，覆盖**实际写入流程**而非仅看 manifest | `app/ops/backup.py`、`tests/unit/test_p9c_ops.py` | 参数化 6 种恶意 member（`articles/../escaped.txt`、嵌套 `..`、`/etc/...`、树外、`C:/`、反斜杠）+ symlink member：`validate_archive` 与 `restore_archive` 均抛 `BackupError`，断言**零文件系统写入**（目标内外都不存在 escaped 文件）与**零 DB 变动** | ✅ |
| R-M01 | Med | `_extract_artifacts` 在检查 image rows 和导入数据库**之前**就把 staging 文件移入 live；随后缺图片或 DB 约束错误只回滚 DB，旧文件已被替换（复现：live 文件 `ORIGINAL` → restore 报 IntegrityError 后仍为 `REPLACED`） | 重排恢复流程为**"先暂存验证、后发布、最后提交 DB、失败可补偿"**：① inner artifacts 全部写入 `{data_dir}/.restore_staging`（live 不动）；② 归档 image 行逐条对照 staging 树校验齐全（缺失 → `BackupError`，**live 与 DB 均不动**）；③ `_promote_artifacts()` 发布时把每个将被覆盖的 live 文件**先移动到 `.restore_rollback` 快照**再落新文件；④ DB 事务提交若失败 → `_compensate_artifacts()` 删除新文件并把快照移回；⑤ 发布本身失败 → 同样补偿后抛错；⑥ `finally` 清理 staging/rollback | `app/ops/backup.py`、`tests/unit/test_p9c_ops.py` | `test_r_m01_db_failure_restores_original_live_files`（**审计原始复现**：篡改 archive 使 image 行 `filename=NULL` → DB 必失败；断言 live 文件恢复为 `ORIGINAL` 且目标 DB 零行）；`test_r_m01_successful_restore_still_overwrites_live_file`（正常路径仍覆盖为 `NEW`，证明补偿机制不破坏成功语义） | ✅ |
| R-M02 | Med | 仅 LLM usage 保留历史；实际付费成本随 reset/重生成丢失：SERP 重试删除 `SerpRun`（含 cost）、图片重规划删除 `ImageRow`（含 cost）、同图坏文件重生成覆盖旧 `provider_cost`、证据核实额外抓取未保存 `ExtractedPage` 成本、共享 source page 无法表示每 job 实际花费 | 新增**独立于 checkpoint 的追加式付费成本台账** `provider_cost_events`（`job_id/provider/step/kind/amount/detail/created_at`，迁移 `0014`）+ `app/services/cost_ledger.py`（`record_provider_cost` / `record_cache_hit` / `record_reuse` / `total_cost`）。埋点：`serp_search`（每次真实 SERP 调用）、`source_extract`（fresh=charged、TTL 命中=cache_hit 0 成本）、`evidence_research`（核实抓取经 `verify_evidence_sources(on_extraction=...)` 记录）、`image_generate`（重生成=charged、有效文件复用=reused 0 成本）。`amount=None` 保持 NULL（未知 ≠ 0）；`checkpoints.reset_from_step` 明确不触碰台账；`cleanup` 随 job 清理台账行 | `app/db/models/cost.py`（新增）、`app/services/cost_ledger.py`（新增）、`migrations/versions/20260918_0014_provider_cost_ledger.py`（新增）、`app/pipeline/steps/{serp_search,source_extract,evidence_research,image_generate}.py`、`app/services/evidence_verification.py`、`app/pipeline/checkpoints.py`、`app/ops/cleanup.py` | `tests/unit/test_r2_cost_ledger.py`（6 用例，驱动**真实步骤**）：SERP 成本跨 `reset_from_step(2)` 保留且重试累加为 3.0；未知成本 NULL 且 `total_cost=None`；fresh 抓取 charged / 缓存命中 cache_hit 且总额不增长；证据核实抓取记入 `evidence_research`；图片重生成追加 charged、复用记 reused 且不重复计费、reset(14) 后历史仍在；`tests/integration/test_generation_job.py` 台账表结构断言；`test_p9c_ops.py` cleanup 断言台账随 job 删除 | ✅ |

**B13 测试结果**：全量 `587 passed, 1 skipped, 1 warning`（B12 后 571 + 新增 16：R-H07/M01 共 9、R-M02 单元 6、台账 schema 1；cleanup 用例为增强既有用例）。
**新增迁移**：`0014_provider_cost_ledger`（新表 `provider_cost_events` + 3 索引）；已 upgrade 本地 PG 测试库，离线 SQL 生成成功。
**关键设计决策**：① 恢复顺序改为"暂存 → 校验 → 发布(带快照) → DB 提交 → 失败补偿"，使"DB 失败后原文件不变"与"文件发布失败后 DB 不变"同时成立；② 付费成本用**独立台账**而非改现有列，历史不可变且与 checkpoint 解耦；未知成本保持 NULL，不伪造 0。

### B14 — 文档 ✅ · R-L01

| ID | 级别 | 问题摘要 | 修复内容 | 关键文件 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| R-L01 | Low | README 与 `docs/AUDIT-FIX-PROGRESS.md` 的"30 项全部完成"超出实际证据；部分说明与代码相反（FAQ 空答案、Upload 顶层数组、完整不可变成本历史） | ① `README.md` 现状段重写；②第一轮文档追加诚实性更正；③本文档补最终状态与 **11 项**逐项表。第三轮再更正 HTML H1、Evidence、失败费用等未覆盖边界 | `README.md`、`docs/AUDIT-FIX-PROGRESS.md`、`docs/AUDIT-R2-FIX-PROGRESS.md`、`docs/audit-r2/` | 历史结果保留；当前验证范围与跳过项见上方第三轮更正 | ✅ 原复现；R3 已更正 |

**B14 测试结果**：文档变更不改代码；全量回归维持 `587 passed, 1 skipped, 1 warning`。
**无新迁移**。
**关键设计决策**：不删除或改写第一轮历史记录（保留其证据与环境说明），而是**就地追加更正块**并明确"最终状态以第二轮文档为准"，符合审计"逐项标注已验证/部分修复/待修复及回归范围"的要求。
