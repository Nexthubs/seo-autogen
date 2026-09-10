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

**P0–P9 全部实现完成 ✅**(P9 可靠性 / 生产化收口:P9-A 核心可靠性、P9-B1 成本、P9-B2 prompt 版本看板、P9-B3 错误 UI、P9-C 运维)。

实现口径:全部阶段的功能已落地并通过自动化测试(`python3 -m pytest`,
含 fake providers 的单元/集成/E2E 路径)。**运行验收**是指用真实外部服务
(真实 LLM / DataForSEO / Exa / Image / Strapi / PostgreSQL / Redis)跑通
一整篇 READY 文章并人工抽查,属于上线前的部署验收步骤,与本仓库的
测试基线相互独立——`/settings` 页面可先行核对各依赖的连通状态。

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
python3 -m pytest            # full suite (baseline: 415 passed, 1 skipped)
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
