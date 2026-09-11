# SEO Article Automation System

Deterministic, observable, checkpointed SEO content production pipeline.

- Backend: FastAPI + Jinja2/HTMX
- DB: PostgreSQL + SQLAlchemy 2.x + Alembic
- Queue: Redis + RQ
- LLM: OpenAI-compatible API (default: local llama.cpp + Qwen3.8-27B)
- SERP: DataForSEO
- Extractor: Exa (Tavily in V1.1)
- Images: OpenAI-compatible Images API (gpt-image-2)
- CMS: Strapi — **Draft only, publish is always manual**

The engineering baseline is `SEO-AUTO-DEV-SPEC.md`; content rules live in
`prompts/seo_article_guideline.md`. Development proceeds strictly by phase:
P0 → P1 → … → P9.

## Current phase

**P0–P9 功能全部实现并通过自动化测试 ✅。**

审计修复按三轮复验记录，各轮编号相互独立:

- **第一轮**(`CODEX-AUDIT-REPORT.md` 基于 commit `c5c7101`,30 项:0 Critical /
  12 High / 15 Medium / 3 Low)——B1–B7 逐批修复(见
  `docs/AUDIT-FIX-PROGRESS.md`)。**该轮"30 项全部修复"的结论已被第二轮审计
  证伪**:其中 9 项实际只是"部分修复",详见下表的诚实更正。
- **第二轮**(同一 `CODEX-AUDIT-REPORT.md` 复验版,commit `0636e2e`,新编号
  `R-H01`…`R-L01`,共 0 Critical / **7 High** / **3 Medium** / **1 Low**)——
  B8–B14 逐批修复,本轮 commit `4de7ab4`,逐项进度与测试证据见
  `docs/AUDIT-R2-FIX-PROGRESS.md`,每阶段详档见 `docs/audit-r2/`。
- **第三轮复验**（当前 `CODEX-AUDIT-REPORT.md`，基于 HEAD `92a919e`）发现
  R2 修复仍有 2 High / 2 Medium / 1 Low 边界。当前工作树已逐项补齐；这些是
  R2 原复现之外的后续修复，不回写成 R2 当时已经覆盖。

| 批次 | 阶段 | 修复项 | 阶段文档 |
|---|---|---|---|
| B8 | P0 | R-H01 RQ `job_timeout` | `docs/audit-r2/B8-P0-R-H01.md` |
| B9 | P1 / P2 | R-H04 Provider raw 类型/脱敏 | `docs/audit-r2/B9-P1P2-R-H04.md` |
| B10 | P4 | R-H06 Evidence 支持性核实 | `docs/audit-r2/B10-P4-R-H06.md` |
| B11 | P5 | R-H05 DoD 门禁 + R-M03 审核血缘 | `docs/audit-r2/B11-P5-R-H05-R-M03.md` |
| B12 | P7 | R-H02 Strapi Upload + R-H03 重试幂等 | `docs/audit-r2/B12-P7-R-H02-R-H03.md` |
| B13 | P9 | R-H07 恢复路径 + R-M01 恢复原子性 + R-M02 成本台账 | `docs/audit-r2/B13-P9-R-H07-R-M01-R-M02.md` |
| B14 | 文档 | R-L01 诚实口径(本文件 + 两份进度文档) | `docs/AUDIT-R2-FIX-PROGRESS.md` |

### 第一轮 30 项的第二轮复验状态(诚实更正)

| 第一轮宣称 | 第二轮实际 | 第二轮修复批次 |
|---|---|---|
| H06 "关闭" | FAQ 空答案 / 有效 H1 / 旧 writer 审核仍放行 → **R-H05** | B11 |
| H03 "部分修复" | Upload 顶层数组仍失败 → **R-H02** | B12 |
| H08 "关闭" | RQ 参数名错误(任务入口前失败)→ **R-H01** | B8 |
| H09 "部分修复" | 只证明有正文,未核实论文/数字 → **R-H06** | B10 |
| H11 "部分修复" | 文章版本已保留,审核历史仍被删除 → **R-M03** | B11 |
| H12 "部分修复" | 恢复路径越界 + 失败覆盖 live 文件 → **R-H07 / R-M01** | B13 |
| M01 "部分修复" | 标准上传异常未落失败态;有行无 ID 重试重复 → **R-H02 / R-H03** | B12 |
| M11 "部分修复" | dict raw 使失败处理再次崩溃 → **R-H04** | B9 |
| M12 "部分修复" | SERP/图片/证据抓取成本仍丢失 → **R-M02** | B13 |
| L03 "关闭" | 上述"全部修复"表述超出证据 → **R-L01** | B14 |

其余 21 项(含 H01/H02/H04/H05/H07/H10/M02–M10/M13–M15/L01/L02)在第二轮
复验中维持"已关闭"。第二轮不复改为通过的部分见下文验收边界。

实现口径与验收边界(诚实声明):

- **代码实现 + 审计修复(当前离线验证)**:P0–P9 全部阶段功能已落地;第一轮 30 项与
  第二轮 **11 项**均保留其当时的测试记录。第三轮边界修复后，本机 Python 3.12
  完整可执行套件为 **`556 passed, 48 skipped, 1 warning`**；48 项是 PostgreSQL /
  真实外部服务条件未满足而跳过，未计作通过。R2 的 `587 passed, 1 skipped` 是另一
  环境的历史结果，不代表本轮执行结果。
- **mock 范围(测试口径)**:自动化测试使用 **fake providers**(fake LLM /
  SERP / Extractor / Image / Strapi)+ 本地 PostgreSQL 验证**逻辑正确性**,
  不调用真实付费 API、不写真实 Strapi(本仓库约定,见 `AGENTS.md`)。
- **live 运行验收(独立步骤,尚未完成)**:用真实外部服务(真实 LLM /
  DataForSEO / Exa / Image / Strapi / PostgreSQL / Redis)跑通一整篇 READY
  文章并人工抽查 Strapi Draft,属于**上线前的部署验收**,与本仓库测试基线
  相互独立。`/settings` 页面可先行核对各依赖的连通状态。
- **未在本轮证明的项(非缺陷、非本轮失败原因)**:真实 PostgreSQL 迁移/集成、
  真实 Redis Worker、真实 Provider 全链路至 READY、专用 Strapi Draft 的 §64
  人工检查——均属延期检查项。

P9-C 运维提供两个手动 CLI(默认 dry-run,幂等,无自动/后台删除):

```bash
# 清理过期终态 job(DB 行 + 本地图片目录);默认 dry-run,--apply 才真正删
python3 -m app.ops.cleanup            # dry-run,预览会删哪些 job / 多少行
python3 -m app.ops.cleanup --apply    # 真正删除(仅 failed/cancelled 且过保留期)

# 备份:生成时间戳 tar.gz(DB JSONL + 图片 artifacts),自动保留 BACKUP_KEEP 份
python3 -m app.ops.backup create
python3 -m app.ops.backup list
python3 -m app.ops.backup restore <archive> [--clear] [--target URL] [--data-dir DIR]
```

> 恢复安全性(第二轮 R-H07/R-M01):归档 member 路径在任何写入前完成规范化与
> containment 校验(拒绝 traversal / 绝对路径 / symlink),非法归档零写入;图片先
> 暂存并通过校验,再带旧文件快照发布,最后提交 DB——任何失败都会把 DB 与原文件
> 一并还原。

Per-phase task breakdown and completion results: see `docs/PHASE-LOG.md`.

## Quick start (local dev, no Docker)

```bash
# 1. database + redis must be reachable (adjust .env as needed)
cp .env.example .env

# 2. install
pip install -e ".[dev]"

# 3. migrate
alembic upgrade head

# 4. run web
uvicorn app.main:app --reload --port 8080

# 5. run worker (separate terminal)
python -m app.workers.article_worker

# 6. tests
python3 -m pytest            # full suite; current result is recorded above
```

> **Migrations (concurrency note).** Schema changes are pure Alembic and are
> applied by an operator (`alembic upgrade head`), never by the web/worker
> processes at import or request time — there is no in-process DDL. Running
> `alembic upgrade head` twice (or against an already-migrated DB) is a safe
> no-op, and a single migration process is always safe to run; what to avoid
> is two *concurrent* `alembic upgrade head` invocations against the same
> database, since Alembic does not coordinate them (each process re-checks
> `alembic_version` on connect; a stale reader could re-apply a revision).
> Keep migrations single-process: run one operator command at a time.

## Docker

```bash
cp .env.example .env
docker compose up -d
# then run migrations inside the web container:
docker compose exec web alembic upgrade head
curl http://localhost:8080/health
```

`GET /health` returns:

```json
{
  "status": "ok",
  "database": true,
  "redis": true
}
```
