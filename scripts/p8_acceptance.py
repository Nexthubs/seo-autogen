#!/usr/bin/env python3
"""P8 acceptance: 浏览器闭环验收（SEO-AUTO-DEV-SPEC.md 43 / 44 / 45, P8）。

验收路径（43.1）:
  关键词 -> Generate -> 浏览器关闭 -> Worker 继续运行 -> 回 Job 页
  -> Preview -> Push Draft -> Strapi（Fake CMS，人工审核前的 Draft 状态）。

环境约束（本机）:
  - LLM endpoint (http://host.docker.internal:1234/v1) 不可达、
    STRAPI_API_TOKEN 为空 —— 因此全部 provider 使用脚本化 Fake
    （复用 tests/integration/test_p8_pipeline.py 的 15 步 payload），
    但以下环节全部走真实链路:
      * 真实 PostgreSQL (seo-pg:5434) + 真实 Redis (6380) + 真实 RQ 队列
      * 真实 FastAPI app（TestClient），POST /generate -> 303
      * 真实 RQ Worker burst 执行真实 task（process_job / sync_strapi_draft）
      * 真实 Strapi 同步 step（run_strapi_sync: 两阶段 A-F + 9 字段校验）
  - 若配置了真实 LLM / DataForSEO / Exa / Strapi 凭据，同一条验收
    路径可以替换 Fake 直接对真实 provider 运行（P8 不依赖 CLI）。

Run（从仓库根目录）:
  PYTHONPATH=/home/ubuntu/ai-coding/seo-autogen /usr/bin/python3 scripts/p8_acceptance.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# DATA_DIR 必须在任何 app.* import 之前指向隔离目录:
# app.db.session 在 import 时构建 engine；preview / job-images 路由在请求时
# 读取 get_settings().data_dir；RQ task 运行时读同一份缓存 settings。
# ---------------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="p8accept-"))
os.environ["DATA_DIR"] = str(TMP)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# 复用 P8 集成测试的脚本化 fixtures（15 步 payload + 4 个 Fake provider），
# 保证验收与测试不漂移。tests/integration 无 __init__.py -> spec_from_file。
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "p8fix", REPO / "tests" / "integration" / "test_p8_pipeline.py"
)
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from rq import Worker as RQWorker  # noqa: E402
from sqlalchemy import delete as sa_delete, select  # noqa: E402

from app.core.enums import StrapiSyncStatus  # noqa: E402
from app.db.models.article import ArticleVersionRow  # noqa: E402
from app.db.models.images import ImageRow  # noqa: E402
from app.db.models.job import GenerationJob  # noqa: E402
from app.db.models.keyword import Keyword, KeywordCluster  # noqa: E402
from app.db.models.strapi_syncs import StrapiSyncRow  # noqa: E402
from app.db.session import SessionLocal, check_database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.pipeline.orchestrator import PipelineProviders  # noqa: E402
from app.schemas.strapi import MediaUploadResult, StrapiBlogEntry  # noqa: E402
from app.workers import article_tasks  # noqa: E402
from app.routes import providers as providers_route  # noqa: E402

KEYWORD = fix.KEYWORD
MARKER = fix.MARKER
CLUSTER_SHEET = "p8acc-cluster"
WORKBOOK_NAME = "p8accept-keywords.xlsx"
STRAPI_DOC_ID = "doc-p8accept"
ADMIN_URL = "https://cms.example.com/admin"

# RQ 2.12 forks a child process per job. In-memory provider records (call
# counts, call sequences) accumulate in the child and are lost when it exits;
# the durable side effects (DB rows, files) are shared. To assert on provider
# behaviour across the fork we record to files under DATA_DIR (shared fs).
LLM_CALLS_FILE = TMP / "p8accept_llm_calls"
STRAPI_CALLS_FILE = TMP / "p8accept_strapi_calls.json"

# ---------------------------------------------------------------------------
# step / note helpers
# ---------------------------------------------------------------------------
_pass = 0
_fail = 0


def step(name: str, ok: bool, detail: str = "") -> bool:
    global _pass, _fail
    if ok:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return ok


def note(name: str, detail: str = "") -> None:
    print(f"  [note] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# FakeStrapiCMS: 实现 run_strapi_sync 用到的 provider 方法（两阶段 A-F）
# ---------------------------------------------------------------------------
class FakeStrapiCMS:
    """记录调用顺序；create 一次、update 记录、get_draft 返回 9 字段齐全。"""

    def __init__(self, settings=None) -> None:
        self._settings = settings
        self.entry: StrapiBlogEntry | None = None
        self.calls: list[tuple] = []

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass

    async def find_blogs_by_slug(self, slug: str) -> list[StrapiBlogEntry]:
        self.calls.append(("find_blogs_by_slug", slug))
        # 无 slug 冲突（本环境无其他 entry）
        return []

    async def create_draft_entry(self, payload: dict) -> StrapiBlogEntry:
        self.calls.append(("create_draft_entry", payload["data"].get("slug")))
        data = payload["data"]
        self.entry = StrapiBlogEntry(
            id=1,
            document_id=STRAPI_DOC_ID,
            slug=data.get("slug"),
            title=data.get("title"),
            status="draft",
            body=data.get("body"),
            meta_title=data.get("metaTitle"),
            meta_description=data.get("metaDescription"),
            seo_keywords=data.get("seoKeywords"),
            author=data.get("author"),
            category=data.get("category"),
            main_image=None,
        )
        return self.entry

    async def update_draft_entry(self, document_id: str, payload: dict) -> StrapiBlogEntry:
        self.calls.append(("update_draft_entry", document_id))
        assert self.entry is not None, "update before create"
        self.entry = self.entry.model_copy(
            update={"body": payload["data"].get("body", self.entry.body)}
        )
        return self.entry

    async def get_draft(self, document_id: str) -> StrapiBlogEntry:
        self.calls.append(("get_draft", document_id))
        assert self.entry is not None, "get_draft before create"
        return self.entry

    async def upload_hero(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        blog_numeric_id: int,
        alt_text: str = "",
        caption: str = "",
    ) -> MediaUploadResult:
        self.calls.append(("upload_hero", blog_numeric_id, filename))
        assert self.entry is not None
        self.entry = self.entry.model_copy(
            update={"main_image": "/uploads/hero.webp"}
        )
        return MediaUploadResult(
            media_id=101,
            url="/uploads/hero.webp",
            document_id="media-hero-p8accept",
            alternative_text=alt_text,
        )

    async def upload_inline(
        self, file_bytes: bytes, filename: str, *, alt_text: str = ""
    ) -> MediaUploadResult:
        self.calls.append(("upload_inline", filename))
        return MediaUploadResult(
            media_id=102, url=f"/uploads/{filename}", alternative_text=alt_text
        )

    async def list_authors(self, page_size: int = 10) -> list[dict]:
        return []

    async def list_categories(self, page_size: int = 10) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# 文件级调用记录: RQ 2.12 每 job fork 子进程，子进程的内存记录（calls）
# 在子进程退出时丢失，只有写入共享文件系统的记录能在父进程读到。
# 下面两个包装器在每次 provider 调用时把记录追加到 DATA_DIR 下的文件。
# ---------------------------------------------------------------------------
class RecordingFakeLLM(fix.FakeLLM):
    """FakeLLM that also appends every call to LLM_CALLS_FILE (survives fork)."""

    def __init__(self, payloads, settings) -> None:
        super().__init__(payloads, settings)
        LLM_CALLS_FILE.write_text("")

    def _handle(self, request) -> "httpx.Response":  # type: ignore[name-defined]
        response = super()._handle(request)
        # append one non-empty line per call (the forked child exits right
        # after the pipeline, so flush + fsync or the buffered line is lost;
        # the line must be non-blank — _llm_call_count() skips empty lines).
        with LLM_CALLS_FILE.open("a") as fh:
            fh.write(f"call {len(self.calls)}\n")
            fh.flush()
            os.fsync(fh.fileno())
        return response


class RecordingFakeStrapiCMS:
    """FakeStrapiCMS whose call log is mirrored to STRAPI_CALLS_FILE (fork-safe)."""

    def __init__(self, settings=None) -> None:
        self._inner = FakeStrapiCMS(settings=settings)

    @property
    def entry(self):
        return self._inner.entry

    @property
    def calls(self):
        return self._inner.calls

    def _record(self, *parts) -> None:
        self._inner.calls.append(tuple(parts))
        # keep native types (int blog_numeric_id must stay an int in JSON);
        # flush + fsync because the forked child exits right after the sync.
        payload = json.dumps(list(parts), ensure_ascii=False)
        with STRAPI_CALLS_FILE.open("a") as fh:
            fh.write(payload + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass

    async def find_blogs_by_slug(self, slug: str):
        self._record("find_blogs_by_slug", slug)
        return await self._inner.find_blogs_by_slug(slug)

    async def create_draft_entry(self, payload: dict):
        self._record("create_draft_entry", payload["data"].get("slug"))
        return await self._inner.create_draft_entry(payload)

    async def update_draft_entry(self, document_id: str, payload: dict):
        self._record("update_draft_entry", document_id)
        return await self._inner.update_draft_entry(document_id, payload)

    async def get_draft(self, document_id: str):
        self._record("get_draft", document_id)
        return await self._inner.get_draft(document_id)

    async def upload_hero(self, file_bytes, filename, *, blog_numeric_id,
                          alt_text="", caption=""):
        self._record("upload_hero", blog_numeric_id, filename)
        return await self._inner.upload_hero(
            file_bytes, filename, blog_numeric_id=blog_numeric_id,
            alt_text=alt_text, caption=caption,
        )

    async def upload_inline(self, file_bytes, filename, *, alt_text=""):
        self._record("upload_inline", filename)
        return await self._inner.upload_inline(file_bytes, filename, alt_text=alt_text)

    async def list_authors(self, page_size: int = 10) -> list[dict]:
        return []

    async def list_categories(self, page_size: int = 10) -> list[dict]:
        return []


def _llm_call_count() -> int:
    if not LLM_CALLS_FILE.exists():
        return 0
    return sum(1 for line in LLM_CALLS_FILE.read_text().splitlines() if line.strip())


def _strapi_calls() -> list[tuple]:
    if not STRAPI_CALLS_FILE.exists():
        return []
    out = []
    for line in STRAPI_CALLS_FILE.read_text().splitlines():
        if line.strip():
            out.append(tuple(json.loads(line)))
    return out


def _reset_strapi_calls() -> None:
    STRAPI_CALLS_FILE.write_text("")


# Provider status 端点使用的 shim: 健康检查一律 True（确定性，不打外网）。
class _StatusShim:
    def __init__(self, inner) -> None:
        self._inner = inner

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()


def main() -> int:
    global _pass, _fail
    print(f"P8 acceptance — DATA_DIR={TMP}")

    # ================= 0. 前置条件 =================
    print("\n== 0. 前置条件 ==")
    step("PostgreSQL 可达", check_database())
    import redis as _redis

    step(
        "Redis 可达 (127.0.0.1:6380)",
        _redis.Redis.from_url("redis://127.0.0.1:6380/0", socket_connect_timeout=3).ping(),
    )
    step("htmx 静态资源存在", (REPO / "app" / "static" / "htmx.min.js").is_file())

    queue = article_tasks.get_queue()
    # 清理上一次残留的队列消息，保证本次断言精确
    queue.empty()

    # 清理同 keyword 的历史 job（上次运行未清干净的保险丝）
    with SessionLocal() as session:
        stale = [
            j.id
            for j in session.scalars(
                select(GenerationJob).where(GenerationJob.keyword == KEYWORD)
            ).all()
        ]
    for stale_id in stale:
        note("清理历史 job", str(stale_id))
        fix._cleanup(stale_id, TMP)

    # fixtures: 15 步脚本化 payload + 4 个 Fake provider（cms=None 走 pipeline 腿）
    providers, llm, serp, extractor, image, fix_settings = fix._providers(Path(TMP))
    # RQ 2.12 forks a child per job — in-memory call records die with the
    # child. Re-bind the pipeline LLM to the file-recording subclass (same
    # scripted payloads) and reset the call log before the burst.
    llm = RecordingFakeLLM([json.dumps(p) for p in fix._payloads()], fix_settings)
    providers.llm = llm
    note("FakeLLM endpoint", fix_settings.llm_base_url)

    job_id: uuid.UUID | None = None

    try:
        # ================= 1. 新建文章 -> Generate =================
        print("\n== 1. 新建文章（43.1）: POST /generate -> 303 -> RQ 队列 ==")
        client = TestClient(create_app())
        r = client.post(
            "/generate",
            data={
                "keyword": KEYWORD,
                "language": "en",
                "market": "US",
                "target_function": "coach",
                "strategy": "auto",
                "image_mode": "auto",
                "author_document_id": "auth-p8accept",
                "category_document_id": "cat-p8accept",
            },
            follow_redirects=False,
        )
        step("POST /generate 返回 303", r.status_code == 303, f"got {r.status_code}")
        job_id = uuid.UUID(r.headers["location"].rsplit("/", 1)[-1])
        note("job_id", str(job_id))

        with SessionLocal() as session:
            job = session.get(GenerationJob, job_id)
            queued_ok = job is not None and job.status == "queued"
            author_ok = job is not None and job.author_document_id == "auth-p8accept"
            category_ok = job is not None and job.category_document_id == "cat-p8accept"
        step("DB 中 job 已落库且 status=queued", queued_ok)
        step("author/category documentId 已记录（Strapi 预检查用）",
             author_ok and category_ok)
        step(
            "RQ 队列有且仅有 1 条 process_job 消息",
            len(queue.jobs) == 1
            and queue.jobs[0].func_name == "app.workers.article_tasks.process_job",
            f"queue.jobs={len(queue.jobs)}",
        )

        # ================= 2. 浏览器关闭 =================
        print("\n== 2. 浏览器关闭（43.1: 关浏览器不中断 job） ==")
        del client  # 不 drain 队列: 消息留在 Redis，等真实 worker 消费
        note("TestClient 已关闭，队列消息仍在 Redis")

        # ================= 3. Worker 继续运行（真实 RQ burst） =================
        print("\n== 3. Worker 继续运行: RQ burst 执行真实 process_job ==")
        article_tasks.build_providers = lambda settings=None: providers
        RQWorker([queue]).work(burst=True)

        with SessionLocal() as session:
            job = session.get(GenerationJob, job_id)
            status_ok = job is not None and job.status == "ready"
            error = (job.error_code, job.error_message) if job else None
        step("job status = ready（浏览器已关，worker 独立完成）", status_ok,
             str(error))
        step("LLM 恰好被调用 15 次", _llm_call_count() == 15,
             f"got {_llm_call_count()}")

        job_dir = TMP / "articles" / str(job_id)
        step("article.md 已导出", (job_dir / "article.md").is_file())
        step("article.json 已导出", (job_dir / "article.json").is_file())
        step("hero 图片已生成 (images/hero.webp)",
             (job_dir / "images" / "hero.webp").is_file())

        with SessionLocal() as session:
            version = session.scalar(
                select(ArticleVersionRow)
                .where(ArticleVersionRow.job_id == job_id)
                .order_by(ArticleVersionRow.version.desc())
            )
            sync_row = session.scalar(
                select(StrapiSyncRow).where(StrapiSyncRow.job_id == job_id)
            )
        title_ok = version is not None and version.title == fix.OUTLINE_P8["title"]
        slug_ok = version is not None and version.slug == "revised-slug"
        stage_ok = version is not None and version.stage == "revision"
        marker_ok = version is not None and MARKER in (version.body_markdown or "")
        no_h1 = version is not None and not (version.body_markdown or "").lstrip().startswith("# ")
        step("最终版本 stage=revision", stage_ok,
             str(version.stage if version else None))
        step(
            "title = 大纲标题（LLM 自报标题被忽略）",
            title_ok, str(version.title if version else None),
        )
        step("slug = normalize_slug(LLM 建议, 标题) -> revised-slug", slug_ok,
             str(version.slug if version else None))
        step("正文保留纯文本 marker " + MARKER, marker_ok)
        step("正文无 H1（5.1 规则）", no_h1)
        step("pipeline 腿不产生 StrapiSyncRow", sync_row is None)

        # ================= 4. 页面与 API =================
        print("\n== 4. 页面与 API（43.2 / 43.3 / 43.4 / 20 / 44） ==")
        client = TestClient(create_app())

        r = client.get("/")
        step("GET / 200 + New Article", r.status_code == 200 and "New Article" in r.text)

        r = client.get("/jobs")
        step(
            "GET /jobs 200: keyword + ready 徽章 + Open",
            r.status_code == 200
            and KEYWORD in r.text
            and 'badge-terminal">ready' in r.text
            and 'href="/jobs/%s"' % job_id in r.text,
        )

        r = client.get(f"/jobs/{job_id}")
        step(
            "GET /jobs/{id} 200 + HTMX 2.5s 轮询",
            r.status_code == 200
            and f'hx-get="/jobs/{job_id}/fragment"' in r.text
            and "every 2.5s" in r.text,
        )

        r = client.get(f"/jobs/{job_id}/fragment")
        frag = r.text
        step("fragment 200 + badge ready", r.status_code == 200 and "ready" in frag)
        step("fragment: Push Draft to Strapi 按钮", "Push Draft to Strapi" in frag)
        step("fragment: ready 状态无 Retry", "Retry" not in frag)
        step("fragment: ready 状态无 Cancel", "Cancel" not in frag)
        step("fragment: Back to Jobs", "Back to Jobs" in frag)
        step("fragment: Strapi 卡片 'No Strapi draft yet.'",
             "No Strapi draft yet." in frag)

        r = client.get(f"/articles/{job_id}")
        step(
            "GET /articles/{id} 200: 标题 + slug + marker",
            r.status_code == 200
            and fix.OUTLINE_P8["title"] in r.text
            and "revised-slug" in r.text
            and MARKER in r.text,
        )
        step(
            "Preview hero 图 src=/static/job-images/{id}/hero.webp",
            f"/static/job-images/{job_id}/hero.webp" in r.text,
        )

        r = client.get(f"/static/job-images/{job_id}/hero.webp")
        step("hero 静态路由 200 + 有字节",
             r.status_code == 200 and len(r.content) > 0,
             f"status={r.status_code} len={len(r.content)}")

        # ---- 关键词数据集（spec 20 / 43.5） ----
        r = client.get("/keywords")
        step("GET /keywords 200", r.status_code == 200 and "Keyword Dataset" in r.text)

        wb = Workbook()
        ws = wb.active
        ws.title = CLUSTER_SHEET
        ws.append(["Keyword", "Search Volume", "KD %", "CPC", "Search Intent"])
        for row in (
            ("p8acc anxious attachment no contact guide", 1200, 18, 2.5, "informational"),
            ("p8acc how long should you no contact", 700, 12, 1.8, "informational"),
            ("p8acc anxious attachment no contact coach", 320, 8, 3.1, "commercial"),
        ):
            ws.append(list(row))
        buf = io.BytesIO()
        wb.save(buf)
        r = client.post(
            "/keywords/import",
            files={"file": (WORKBOOK_NAME, buf.getvalue(),
                            "application/vnd.openxmlformats-officedocument"
                            ".spreadsheetml.sheet")},
        )
        step(
            "POST /keywords/import 200 + 报告",
            r.status_code == 200
            and WORKBOOK_NAME in r.text
            and "3 created" in r.text
            and "import_error" not in r.text,
        )

        with SessionLocal() as session:
            cluster = session.scalar(
                select(KeywordCluster)
                .where(KeywordCluster.sheet_name == CLUSTER_SHEET)
            )
            kw_count = (
                len(
                    session.scalars(
                        select(Keyword).where(Keyword.cluster_id == cluster.id)
                    ).all()
                )
                if cluster is not None
                else 0
            )
        step("DB: cluster 落库 + 3 条 keyword", cluster is not None and kw_count == 3,
             f"cluster={cluster is not None}, kw={kw_count}")

        r = client.get("/keywords?strategy=auto")
        step(
            "GET /keywords?strategy=auto 展示导入行",
            r.status_code == 200
            and CLUSTER_SHEET in r.text
            and "p8acc how long should you no contact" in r.text,
        )

        r = client.get("/api/datasets/keywords?strategy=auto")
        data = r.json()
        hit = [
            k for k in data.get("keywords", [])
            if k["keyword"] == "p8acc how long should you no contact"
        ]
        step(
            "GET /api/datasets/keywords: 含导入 keyword + 指标",
            r.status_code == 200
            and len(hit) == 1
            and hit[0]["cluster"] == CLUSTER_SHEET
            and hit[0]["volume"] == 700
            and hit[0]["kd"] == 12.0,
            json.dumps(data)[:200] if r.status_code == 200 else str(r.status_code),
        )

        # ---- Job REST API（45） ----
        r = client.get(f"/api/jobs/{job_id}")
        data = r.json()
        step(
            "GET /api/jobs/{id}: status=ready + progress 15/15",
            r.status_code == 200
            and data["status"] == "ready"
            and data["keyword"] == KEYWORD
            and data["progress"]["stage"] == 15,
            json.dumps(data)[:200],
        )

        r = client.get(f"/api/jobs/{job_id}/article")
        data = r.json()
        art = data.get("article") or {}
        step(
            "GET /api/jobs/{id}/article: revision + links_valid",
            r.status_code == 200
            and art.get("stage") == "revision"
            and data.get("links_valid") is True
            and MARKER in (art.get("body_markdown") or "")
            and MARKER in (art.get("rendered_markdown") or ""),
        )

        r = client.get(f"/api/jobs/{job_id}/serp")
        data = r.json()
        results = (data.get("serp") or {}).get("results") or []
        step(
            "GET /api/jobs/{id}/serp: 8 organic + 1 paa + 1 related = 10 条",
            r.status_code == 200
            and len(results) == 10
            and data["serp"]["query"] == KEYWORD,
            f"got {len(results)}",
        )

        r = client.get(f"/api/jobs/{job_id}/sources")
        data = r.json()
        step(
            "GET /api/jobs/{id}/sources: 5 条来源",
            r.status_code == 200 and len(data.get("sources", [])) == 5,
        )

        r = client.get(f"/api/jobs/{job_id}/reviews")
        data = r.json()
        types = {x["review_type"] for x in data.get("reviews", [])}
        step(
            "GET /api/jobs/{id}/reviews: seo/fact/style/anticopy",
            r.status_code == 200
            and types == {"seo", "fact", "style", "anticopy"},
            str(types),
        )

        # ================= 5. Provider 状态（44: 只报可达性） =================
        print("\n== 5. GET /api/providers/status（44） ==")
        cms_status = FakeStrapiCMS()
        original_build = providers_route.build_providers

        def _status_build(settings=None) -> PipelineProviders:
            return PipelineProviders(
                llm=_StatusShim(llm),
                serp=_StatusShim(serp),
                extractor=_StatusShim(extractor),
                image=_StatusShim(image),
                cms=_StatusShim(cms_status),
            )

        providers_route.build_providers = _status_build
        try:
            r = client.get("/api/providers/status")
            data = r.json()
        finally:
            providers_route.build_providers = original_build
        names = ("llm", "serp", "extractor", "image", "cms")
        all_reach = (
            r.status_code == 200
            and set(data.get("providers", {})) == set(names)
            and all(
                data["providers"][n] == {"reachable": True} for n in names
            )
        )
        step("5 个 provider 均 reachable=true（确定性 Fake 健康检查）", all_reach,
             json.dumps(data)[:200] if r.status_code == 200 else str(r.status_code))
        step("响应不含任何密钥/URL（60）",
             all("http" not in json.dumps(data["providers"]) and "token" not in json.dumps(data["providers"]).lower()
                 for _ in (0,)))

        # ================= 6. Push Draft to Strapi（43.5） =================
        print("\n== 6. Push Draft to Strapi（43.5: 只入队，真实 worker 执行） ==")
        r = client.post(f"/jobs/{job_id}/sync-strapi")
        data = r.json()
        step(
            "POST /jobs/{id}/sync-strapi 200 + enqueued",
            r.status_code == 200
            and data.get("job_id") == str(job_id)
            and data.get("enqueued") is True,
            json.dumps(data),
        )

        _reset_strapi_calls()
        fake_cms = RecordingFakeStrapiCMS()
        article_tasks.StrapiCMSProvider = lambda settings=None: fake_cms
        RQWorker([queue]).work(burst=True)

        with SessionLocal() as session:
            sync_row = session.scalar(
                select(StrapiSyncRow).where(StrapiSyncRow.job_id == job_id)
            )
            job = session.get(GenerationJob, job_id)
            hero_row = session.scalar(
                select(ImageRow)
                .where(ImageRow.job_id == job_id, ImageRow.role == "hero")
            )
        sync_ok = (
            sync_row is not None
            and sync_row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
            and sync_row.strapi_document_id == STRAPI_DOC_ID
            and sync_row.strapi_id == 1
        )
        step(
            "StrapiSyncRow: draft_created + documentId + strapi_id=1",
            sync_ok,
            f"status={getattr(sync_row, 'sync_status', None)} "
            f"doc={getattr(sync_row, 'strapi_document_id', None)} "
            f"err={getattr(sync_row, 'error_message', None)}",
        )
        step("job status = strapi_draft_created",
             job is not None and job.status == "strapi_draft_created",
             str(job.status if job else None))
        step(
            "hero ImageRow 写入 strapi_media_id + strapi_url",
            hero_row is not None
            and hero_row.strapi_media_id == 101
            and hero_row.strapi_url == "https://cms.example.com/uploads/hero.webp",
            f"media_id={getattr(hero_row, 'strapi_media_id', None)} "
            f"url={getattr(hero_row, 'strapi_url', None)}",
        )

        strapi_calls = _strapi_calls()  # read back from the shared file
        call_names = [c[0] for c in strapi_calls]
        step("调用序: find_blogs_by_slug 最先（42 冲突检查先于写）",
             call_names and call_names[0] == "find_blogs_by_slug", str(call_names))
        step("create_draft_entry 恰好 1 次（41 幂等锚点）",
             call_names.count("create_draft_entry") == 1, str(call_names))
        upload_calls = [c for c in strapi_calls if c[0] == "upload_hero"]
        step(
            "upload_hero 收到 int blog_numeric_id=1（38 入口链接）",
            len(upload_calls) == 1
            and upload_calls[0][1] == 1
            and isinstance(upload_calls[0][1], int),
            str(upload_calls),
        )
        step("get_draft 最后（64: GET 校验 9 字段）",
             call_names and call_names[-1] == "get_draft", str(call_names))

        r = client.get(f"/jobs/{job_id}/fragment")
        frag = r.text
        step("fragment: badge strapi draft created", "strapi draft created" in frag)
        step("fragment: Draft created in Strapi (doc-p8accept)",
             "Draft created in Strapi" in frag and f"({STRAPI_DOC_ID})" in frag)
        step("fragment: Update Existing Draft 按钮", "Update Existing Draft" in frag)
        step(f"fragment: Open in Strapi -> {ADMIN_URL}",
             f'href="{ADMIN_URL}"' in frag and "Open in Strapi" in frag)
        step("fragment: Cancel 按钮（非终态可取消）", "Cancel" in frag)

        # ---- P8 最小重试语义（43.3: 仅终态可 retry） ----
        r = client.post(f"/jobs/{job_id}/retry")
        step("retry 非终态(strapi_draft_created) -> 409",
             r.status_code == 409, f"got {r.status_code}")

        r = client.post(f"/jobs/{job_id}/cancel")
        data = r.json()
        step("cancel 非终态 -> 200 cancelled",
             r.status_code == 200 and data.get("status") == "cancelled",
             json.dumps(data))
        with SessionLocal() as session:
            job = session.get(GenerationJob, job_id)
        step("DB: status=cancelled",
             job is not None and job.status == "cancelled")

        r = client.post(f"/jobs/{job_id}/retry")
        data = r.json() if r.status_code == 200 else {}
        step(
            "retry 终态(cancelled) -> 200 重新入队",
            r.status_code == 200
            and data.get("status") == "queued"
            and data.get("enqueued") is True,
            json.dumps(data),
        )

    except Exception as exc:  # noqa: BLE001 — 汇总后非零退出
        import traceback

        print(f"\n[FAIL] 未预期异常: {exc}")
        traceback.print_exc()
        _fail += 1
    finally:
        # ================= 7. 收尾（只清理本次 p8test/p8acc 标记的数据） =================
        print("\n== 7. 收尾 ==")
        if job_id is not None:
            fix._cleanup(job_id, TMP)
        with SessionLocal() as session:
            cluster = session.scalar(
                select(KeywordCluster)
                .where(KeywordCluster.sheet_name == CLUSTER_SHEET)
            )
            if cluster is not None:
                session.execute(
                    sa_delete(Keyword).where(Keyword.cluster_id == cluster.id)
                )
                session.delete(cluster)
                session.commit()
        queue.empty()  # 丢弃 retry 重新入队的消息（job 已删）
        shutil.rmtree(TMP, ignore_errors=True)
        note("已清理 job 行 + keyword cluster + 队列 + tmp 目录")

    # ================= 汇总 =================
    total = _pass + _fail
    print(f"\nP8 ACCEPTANCE: {'PASS' if _fail == 0 else 'FAIL'} "
          f"({_pass}/{total} steps passed)")
    if os.environ.get("LLM_API_KEY") or (REPO / ".env").read_text().count("1234") > 0:
        note("本机 LLM endpoint 不可达时，以上验收全部走脚本化 Fake；"
             "配置真实凭据后同一路径可直接打真实 provider。")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
