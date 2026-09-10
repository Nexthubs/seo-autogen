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

**P0–P9 功能全部实现并通过自动化测试 ✅**,随后按 `CODEX-AUDIT-REPORT.md`
(commit `c5c7101`,审计结论:整体验收不通过,0 Critical / 12 High /
15 Medium / 3 Low)逐批修复了全部 30 项审计问题——**B1–B7 已全部完成
并回归测试**(逐项进度、测试证据见 `docs/AUDIT-FIX-PROGRESS.md`):

| 批次 | 阶段 | 修复项 | commit |
|---|---|---|---|
| B1 | P0 / P1 | M04, H08, L02, M09, M11 | `cf85665` |
| B2 | P2 | H01, H02, M15 | `c50c235` |
| B3 | P3 | H04, M05 | `c6a4b83` |
| B4 | P4 / P5 | H09, H06, H07, H11, M08 | `9462af0` |
| B5 | P6 | H05, H10, M06, M07, L01 | `8af9212` |
| B6 | P7 | H03, M01, M02 | `52d3643` |
| B7 | P8 / P9 + 文档 | M13, M14, M12, M10, M03, H12, L03 | `12871b0` |

实现口径与验收边界(诚实声明):

- **代码实现 + 审计修复**:P0–P9 全部阶段功能已落地;上述 30 项审计问题已在
  B1–B7 逐批修复并回归。完整测试套件(unit + integration,本地 PostgreSQL +
  fake providers)全绿。
- **mock 范围(测试口径)**:自动化测试使用 **fake providers**(fake LLM /
  SERP / Extractor / Image / Strapi)+ 本地 PostgreSQL 验证**逻辑正确性**,
  不调用真实付费 API、不写真实 Strapi(本仓库约定,见 `AGENTS.md`)。
- **live 运行验收(独立步骤,尚未完成)**:用真实外部服务(真实 LLM /
  DataForSEO / Exa / Image / Strapi / PostgreSQL / Redis)跑通一整篇 READY
  文章并人工抽查,属于**上线前的部署验收**,与本仓库测试基线相互独立。
  `/settings` 页面可先行核对各依赖的连通状态。

P9-C 运维提供两个手动 CLI(默认 dry-run,幂等,无自动/后台删除):

```bash
# 清理过期终态 job(DB 行 + 本地图片目录);默认 dry-run,--apply 才真正删
python3 -m app.ops.cleanup            # dry-run,预览会删哪些 job / 多少行
python3 -m app.ops.cleanup --apply    # 真正删除(仅 failed/cancelled 且过保留期)

# 备份:生成时间戳 tar.gz(DB JSONL + 图片 artifacts),自动保留 BACKUP_KEEP 份
python3 -m app.ops.backup create
python3 -m app.ops.backup list
python3 -m app.ops.backup restore <archive> [--clear] [--target URL]
```

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
python3 -m pytest            # full suite (current: 520 passed, 1 skipped)
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
