# SEO-AUTO-DEV-SPEC.md

> **Project:** SEO Article Automation System  
> **Spec Version:** 1.0  
> **Status:** Development Baseline  
> **Primary Coding Model:** local llama.cpp / Qwen3.8-27B  
> **Primary SERP Provider:** DataForSEO  
> **Primary CMS:** Strapi  
> **Article Format:** Markdown  
> **Publish Policy:** Draft only; publishing is always manual

---

# 1. 文档目的

本文档是 SEO 自动文章生产系统的工程实现基线，目标是让 Coding Agent 可以按照明确的模块边界、数据结构、接口契约和验收条件逐阶段实现系统，而不是让模型自行推断架构。

本文档与《SEO 文章 AI 生产规范》配套使用：

- `SEO-AUTO-DEV-SPEC.md`：定义**系统怎么实现**
- `prompts/seo_article_guideline.md`：定义**文章应该怎么写**

除非明确更新本规范，否则 Coding Agent 不应为了兼容某一阶段的临时代码而改变核心架构。

---

# 2. 已确定且冻结的 V1 产品决策

以下决策作为 V1 基线。

## 2.1 技术栈

```text
Backend         FastAPI
Web UI          Jinja2 + HTMX
ORM             SQLAlchemy 2.x
Migration       Alembic
Database        PostgreSQL
Queue           Redis + RQ
Config          pydantic-settings
HTTP Client     httpx
Validation      Pydantic v2
Testing         pytest + pytest-asyncio
Deployment      Docker Compose
```

V1 不使用：

```text
React / Next.js
LangChain
LangGraph
Celery
Kubernetes
Vector Database
Microservices
```

---

## 2.2 搜索与正文抓取

```text
Google SERP ranking     DataForSEO
Competitor extraction  Exa
Fallback extractor      Tavily（后续实现）
```

必须严格区分：

```text
SERPProvider
ContentExtractor
```

DataForSEO 决定“Google 当前排名是什么”。

Exa/Tavily 负责“把指定 URL 的正文抓回来”。

不得把 Exa/Tavily Search Result 当成真实 Google Top 5。

---

## 2.3 LLM

文章分析和生产统一通过：

```text
OpenAI-Compatible API
```

V1 默认：

```text
llama.cpp
+
Qwen3.8-27B
```

必须允许通过 `.env` 修改：

```text
API Base URL
API Key
Model Name
Timeout
Temperature
```

Pipeline 不得依赖某个模型专属 SDK。

---

## 2.4 图片生成

```text
Provider     OpenAI-compatible Images API
Model        gpt-image-2
```

图片数量根据最终文章长度动态确定，但：

```text
V1_TOTAL_IMAGE_MAX = 3
```

**这个 3 张包含头图。**

默认规则：

| 最终文章长度 | 总图片数 | 图片构成 |
|---|---:|---|
| `< 1200 words` | 1 | 1 张 Hero |
| `1200–2200 words` | 2 | Hero + 1 张正文图 |
| `> 2200 words` | 3 | Hero + 2 张正文图 |

LLM 的 Image Planner 可以建议更少图片，但绝不能突破 3 张。

---

# 3. 头图 / mainImage 规则

## 3.1 最终决策

**第一张图不是普通正文首图，而是专门生成的 Hero Image。**

系统图片角色：

```text
image[0] -> role=hero
image[1] -> role=inline
image[2] -> role=inline
```

Hero Image：

- 基于文章整体主题生成；
- 上传到 Strapi；
- 绑定到 `Blog.mainImage`；
- 默认不再插入 `body` Markdown。

因此：

```text
1 张图
= mainImage
= body 内无图片

2 张图
= mainImage + 1 张 body inline image

3 张图
= mainImage + 2 张 body inline images
```

---

## 3.2 为什么不把 Hero 再插进正文

当前 Strapi 已存在独立：

```text
mainImage
```

字段。

如果网站 Blog 页面本身已经在文章标题附近渲染 `mainImage`，再把同一张图插入 Markdown 开头会造成：

```text
Hero
↓
Title
↓
同一张 Hero 再出现一次
```

因此 V1 默认：

```env
STRAPI_FRONTEND_RENDERS_MAIN_IMAGE=true
```

当值为 `true`：

```text
Hero -> mainImage only
```

不进入 `body`。

如果未来前端不渲染 `mainImage`，再允许配置：

```env
STRAPI_FRONTEND_RENDERS_MAIN_IMAGE=false
```

由 Markdown Renderer 在正文开头插入 Hero。

---

# 4. 当前 Strapi Blog Content-Type 映射

根据当前 Content-Type，Blog 已包含：

| Strapi Field | Type | 自动化系统映射 |
|---|---|---|
| `title` | Text | Article Title / H1 |
| `slug` | UID | URL Slug |
| `author` | manyToOne → Author | 默认作者或 UI 指定作者 |
| `category` | manyToOne → Category | 默认分类或 UI 指定分类 |
| `postedAt` | Date | 默认不自动写入，可配置 |
| `mainImage` | Media | Hero Image |
| `body` | Rich text (Markdown) | 正文 Markdown，**不包含 H1** |
| `metaDescription` | Text | Meta Description |
| `metaTitle` | Text | SEO Title |
| `seoKeywords` | Text | SEO keyword string |

V1 **不要求修改现有 Blog Content-Type**。

---

# 5. 一个非常重要的 Markdown / H1 规则

内部数据模型不能把文章简单保存为一整段 Markdown。

应该拆成：

```text
title
body_markdown
```

其中：

```text
title
= 唯一 H1 内容

body_markdown
= 从 Introduction 开始
= 不包含 "# Title"
```

原因：

前端通常会把 Strapi 的：

```text
Blog.title
```

渲染为页面 H1。

如果 `body` 里面再保留：

```markdown
# Article Title
```

就会出现两个 H1。

---

## 5.1 本地 Markdown 导出

本地文件：

```text
article.md
```

可以渲染为：

```markdown
---
metaTitle: ...
metaDescription: ...
slug: ...
---

# {{ title }}

{{ body_markdown }}
```

但发送到 Strapi 时：

```text
title -> Blog.title
body_markdown -> Blog.body
```

不得把 H1 放入 `body`。

---

# 6. Strapi 字段详细规则

## 6.1 title

来源：

```text
ContentBrief
→ Outline
→ ArticleWriter
→ Final Article
```

要求：

- 与文章 H1 相同；
- 不等于强制使用 `metaTitle`；
- 可以比 `metaTitle` 更自然、更长。

---

## 6.2 slug

由程序生成，不依赖 LLM 直接输出作为最终值。

流程：

```text
LLM suggested slug
↓
normalize_slug()
↓
lowercase
↓
ASCII / transliteration
↓
kebab-case
↓
strip duplicated hyphens
↓
uniqueness check
```

例如：

```text
Anxious Attachment No Contact
```

转换：

```text
anxious-attachment-no-contact
```

创建 Strapi Draft 前必须检查 slug 是否已经存在。

---

## 6.3 author

当前是：

```text
manyToOne -> Author
```

V1 不允许 LLM 自己生成作者。

配置：

```env
STRAPI_DEFAULT_AUTHOR_DOCUMENT_ID=
```

允许任务级 override：

```text
job.author_document_id
```

优先级：

```text
job override
>
default author
```

如果两者都为空：

```text
STRAPI_AUTHOR_REQUIRED=true
```

时任务在 Strapi Sync 前失败，并给出明确错误。

---

## 6.4 category

当前是：

```text
manyToOne -> Category
```

V1 支持：

```env
STRAPI_DEFAULT_CATEGORY_DOCUMENT_ID=
```

任务也可 override：

```text
job.category_document_id
```

后续 Keyword Dataset 可以增加：

```text
keyword cluster -> Strapi category
```

自动映射。

LLM 不得发明 Category。

---

## 6.5 postedAt

这是自定义 Date，而不是 Strapi 自带的 `publishedAt`。

V1 默认：

```env
STRAPI_SET_POSTED_AT_ON_DRAFT=false
```

原因：

Draft 可能经过数天人工审核，如果 Draft 创建时就写入日期，最终页面展示日期可能早于真正发布时间。

如果当前前端要求此字段非空，可以改成：

```env
STRAPI_SET_POSTED_AT_ON_DRAFT=true
```

并写入 Draft 创建当天日期。

这与 Strapi 自带的 Draft/Publish 状态无关。

---

## 6.6 metaTitle

来自 SEO Metadata Generator。

目标：

```text
50–60 chars 左右
```

但不做超出长度即强制失败，只标记 QA warning。

---

## 6.7 metaDescription

来自 SEO Metadata Generator。

目标：

```text
140–155 chars 左右
```

---

## 6.8 seoKeywords

当前字段是 Text，因此 V1 使用逗号分隔。

来源优先级：

```text
Primary Keyword
Secondary Keywords
Selected Long-tail Keywords
```

例如：

```text
anxious attachment no contact, anxious attachment, no contact rule, attachment anxiety
```

规则：

```text
去重
最多 12 个
最多约 500 字符
```

不把全部 SEMrush 关键词塞进该字段。

---

# 7. 总体架构

```text
┌──────────────────────────────────────────────────────┐
│                      Web UI                          │
│                FastAPI + Jinja + HTMX                │
└───────────────────────┬──────────────────────────────┘
                        │
                        ▼
                 Create Generation Job
                        │
                        ▼
┌──────────────────────────────────────────────────────┐
│                    PostgreSQL                        │
│  Jobs / SERP / Sources / Brief / Versions / Images  │
└───────────────────────┬──────────────────────────────┘
                        │
                        ▼
                   Redis / RQ
                        │
                        ▼
┌──────────────────────────────────────────────────────┐
│                  Article Worker                      │
│                                                      │
│ Keyword Context                                      │
│      ↓                                               │
│ DataForSEO                                           │
│      ↓                                               │
│ Source Extract                                       │
│      ↓                                               │
│ SERP Analyze                                         │
│      ↓                                               │
│ Evidence Research                                    │
│      ↓                                               │
│ Content Brief                                        │
│      ↓                                               │
│ Outline                                              │
│      ↓                                               │
│ Article Writer                                       │
│      ↓                                               │
│ SEO / Fact / Style Review                            │
│      ↓                                               │
│ Reviser                                              │
│      ↓                                               │
│ Image Planner                                        │
│      ↓                                               │
│ GPT-Image-2                                          │
│      ↓                                               │
│ READY                                                │
└───────────────────────┬──────────────────────────────┘
                        │
                   Manual Click
                        │
                        ▼
                  Push to Strapi
                        │
                        ▼
                    Draft only
                        │
                        ▼
                   Human Review
                        │
                        ▼
                 Human Publish
```

---

# 8. Pipeline 状态机

统一状态枚举：

```python
class JobStatus(str, Enum):
    QUEUED = "queued"
    KEYWORD_PREPARING = "keyword_preparing"
    SERP_SEARCHING = "serp_searching"
    SOURCE_EXTRACTING = "source_extracting"
    SERP_ANALYZING = "serp_analyzing"
    EVIDENCE_RESEARCHING = "evidence_researching"
    BRIEF_GENERATING = "brief_generating"
    OUTLINE_GENERATING = "outline_generating"
    ARTICLE_GENERATING = "article_generating"
    SEO_REVIEWING = "seo_reviewing"
    FACT_REVIEWING = "fact_reviewing"
    STYLE_REVIEWING = "style_reviewing"
    ARTICLE_REVISING = "article_revising"
    IMAGE_PLANNING = "image_planning"
    IMAGE_GENERATING = "image_generating"
    READY = "ready"
    STRAPI_SYNCING = "strapi_syncing"
    STRAPI_DRAFT_CREATED = "strapi_draft_created"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

---

# 9. Pipeline Checkpoint 原则

每一步完成后必须：

1. 保存数据库；
2. 保存结构化结果；
3. 更新 Job Status；
4. 提交事务；
5. 再进入下一步。

禁止：

```text
整个 Pipeline 的中间状态只存在 Python 内存
```

因此：

```text
Image Generation 失败
```

时不需要重新：

```text
SERP
→ Crawl
→ Article
```

而只需从 Image Step 重试。

---

# 10. Provider Interface

目录：

```text
app/providers/
```

---

## 10.1 LLMProvider

```python
from typing import TypeVar, Type
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

class LLMProvider:
    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        temperature: float | None = None,
    ) -> T:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError
```

V1：

```text
OpenAICompatibleLLMProvider
```

---

## 10.2 SERPProvider

```python
class SERPProvider:
    async def search(
        self,
        request: "SERPRequest",
    ) -> "SERPResponse":
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError
```

V1：

```text
DataForSEOSERPProvider
```

---

## 10.3 ContentExtractor

```python
class ContentExtractor:
    async def extract(
        self,
        urls: list[str],
    ) -> list["ExtractedPage"]:
        raise NotImplementedError
```

V1：

```text
ExaContentExtractor
```

V1.1：

```text
TavilyContentExtractor
```

---

## 10.4 ImageProvider

```python
class ImageProvider:
    async def generate(
        self,
        request: "ImageGenerationRequest",
    ) -> "GeneratedImage":
        raise NotImplementedError
```

V1：

```text
OpenAIImageProvider
```

---

## 10.5 CMSProvider

```python
class CMSProvider:
    async def health_check(self) -> bool:
        raise NotImplementedError

    async def create_draft(self, article):
        raise NotImplementedError

    async def update_draft(self, document_id, article):
        raise NotImplementedError

    async def upload_media(self, ...):
        raise NotImplementedError
```

V1：

```text
StrapiCMSProvider
```

CMSProvider **不定义 `publish()`**。

这是一项安全约束。

---

# 11. .env.example

```env
# ======================================================
# APP
# ======================================================

APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8080
APP_SECRET_KEY=change-me
APP_TIMEZONE=UTC

DATA_DIR=/app/data

# ======================================================
# DATABASE
# ======================================================

DATABASE_URL=postgresql+psycopg://seo:seo@postgres:5432/seo

# ======================================================
# REDIS / RQ
# ======================================================

REDIS_URL=redis://redis:6379/0
RQ_QUEUE_NAME=seo

# ======================================================
# LLM
# ======================================================

LLM_PROVIDER=openai_compatible

LLM_BASE_URL=http://host.docker.internal:1234/v1
LLM_API_KEY=local
LLM_MODEL=qwen3.8-27b

LLM_TIMEOUT_SECONDS=300
LLM_MAX_RETRIES=2

LLM_TEMPERATURE_ANALYSIS=0.25
LLM_TEMPERATURE_WRITING=0.70
LLM_TEMPERATURE_REVIEW=0.20

# ======================================================
# DATAFORSEO
# ======================================================

SERP_PROVIDER=dataforseo

DATAFORSEO_BASE_URL=https://api.dataforseo.com
DATAFORSEO_LOGIN=
DATAFORSEO_PASSWORD=

DATAFORSEO_LOCATION_CODE=2840
DATAFORSEO_LANGUAGE_CODE=en
DATAFORSEO_DEVICE=desktop
DATAFORSEO_DEPTH=10

# 0 = 不额外点击 PAA，节省成本
DATAFORSEO_PAA_CLICK_DEPTH=0

# V1 不需要为 AI Overview 额外付费
DATAFORSEO_LOAD_ASYNC_AI_OVERVIEW=false
DATAFORSEO_CALCULATE_RECTANGLES=false

# ======================================================
# EXTRACTOR
# ======================================================

CONTENT_EXTRACTOR_PROVIDER=exa

EXA_BASE_URL=https://api.exa.ai
EXA_API_KEY=

TAVILY_BASE_URL=https://api.tavily.com
TAVILY_API_KEY=

SOURCE_CACHE_TTL_HOURS=168
SOURCE_MAX_CHARS=50000

# ======================================================
# IMAGE
# ======================================================

IMAGE_PROVIDER=openai

IMAGE_BASE_URL=https://api.openai.com/v1
IMAGE_API_KEY=
IMAGE_MODEL=gpt-image-2

IMAGE_QUALITY=medium
IMAGE_MAX_COUNT=3

# ======================================================
# STRAPI
# ======================================================

STRAPI_BASE_URL=https://cms.example.com
STRAPI_API_TOKEN=

# 不要在代码里假定一定叫 blogs
STRAPI_BLOG_PLURAL_API_ID=blogs
STRAPI_BLOG_UID=api::blog.blog

STRAPI_DEFAULT_AUTHOR_DOCUMENT_ID=
STRAPI_DEFAULT_CATEGORY_DOCUMENT_ID=

STRAPI_AUTHOR_REQUIRED=true
STRAPI_CATEGORY_REQUIRED=true

STRAPI_SET_POSTED_AT_ON_DRAFT=false
STRAPI_FRONTEND_RENDERS_MAIN_IMAGE=true

# ======================================================
# ARTICLE
# ======================================================

ARTICLE_DEFAULT_LANGUAGE=en
ARTICLE_DEFAULT_MARKET=US
ARTICLE_MIN_IMAGE_COUNT=1
ARTICLE_MAX_IMAGE_COUNT=3

SEO_GUIDELINE_PATH=prompts/seo_article_guideline.md
```

---

# 12. DataForSEO 实现规范

V1 使用：

```text
POST /v3/serp/google/organic/live/advanced
```

认证：

```text
HTTP Basic Auth
username = DATAFORSEO_LOGIN
password = DATAFORSEO_PASSWORD
```

默认请求：

```json
[
  {
    "keyword": "anxious attachment no contact",
    "location_code": 2840,
    "language_code": "en",
    "device": "desktop",
    "depth": 10,
    "calculate_rectangles": false,
    "load_async_ai_overview": false
  }
]
```

如果：

```env
DATAFORSEO_PAA_CLICK_DEPTH > 0
```

再加入：

```json
{
  "people_also_ask_click_depth": 1
}
```

V1 只需要 Google US English，因此默认：

```text
location_code = 2840
language_code = en
```

后续 UI 支持不同市场时，再实现 Location Resolver。

---

## 12.1 DataForSEO 输出解析

必须解析：

```text
organic
people_also_ask
related_searches
featured_snippet（若存在）
```

竞争文章只选择：

```text
organic result
```

并按 Google Organic Rank 排序，取：

```text
Top 5 unique URLs
```

不得把：

```text
paid
video
shopping
local_pack
images
```

算入 Top 5 competitor articles。

---

## 12.2 DataForSEO Raw Response

必须保留完整原始响应：

```text
serp_runs.raw_response JSONB
```

同时解析为标准化结果。

这样以后 Provider Parser 出现问题时可以重新解析，而不需要再次付费请求 SERP。

---

# 13. SERP Domain Models

```python
class SERPRequest(BaseModel):
    keyword: str
    location_code: int
    language_code: str
    device: Literal["desktop", "mobile"] = "desktop"
    depth: int = 10


class OrganicResult(BaseModel):
    rank: int
    title: str
    url: str
    domain: str | None = None
    snippet: str | None = None


class PAAQuestion(BaseModel):
    question: str
    source_url: str | None = None


class SERPResponse(BaseModel):
    keyword: str
    organic_results: list[OrganicResult]
    paa_questions: list[PAAQuestion]
    related_searches: list[str]
    raw: dict
```

---

# 14. URL 去重与 Source Cache

不得让 LLM 做 URL 去重。

程序实现：

```text
normalize_url()
```

规则：

1. lowercase hostname；
2. remove fragment；
3. remove default port；
4. remove trailing `/` where safe；
5. remove tracking query params；
6. sort remaining query params。

删除：

```text
utm_source
utm_medium
utm_campaign
utm_term
utm_content
gclid
fbclid
ref
source
```

保存：

```text
normalized_url
url_hash = SHA256(normalized_url)
```

数据库：

```text
UNIQUE(url_hash)
```

---

## 14.1 内容去重

抓取后：

```text
clean_content
↓
normalize whitespace
↓
content_hash = SHA256(...)
```

若不同 URL：

```text
content_hash 相同
```

视作重复页面。

Top 5 分析时只保留一份内容，并尝试补足下一条 organic result。

---

## 14.2 Cache

默认：

```text
SOURCE_CACHE_TTL_HOURS=168
```

即 7 天。

逻辑：

```text
URL exists?
  ├─ no  -> extract
  └─ yes
       ├─ cache fresh -> use cache
       └─ expired     -> re-extract
```

用户可以在 Job Detail：

```text
Force Refresh Sources
```

但默认不重复抓取。

---

# 15. Content Extraction

Exa 只接收：

```text
DataForSEO 返回的 URL
```

不让 Exa 再决定搜索排名。

输出：

```python
class ExtractedPage(BaseModel):
    url: str
    normalized_url: str
    title: str | None
    content_markdown: str
    extracted_at: datetime
    extractor: str
```

正文超过：

```env
SOURCE_MAX_CHARS
```

时进行可重复的截断策略，不得随机截断。

---

# 16. 竞争文章分析

每篇来源单独生成：

```python
class CompetitorAnalysis(BaseModel):
    source_id: UUID
    content_type: str
    search_intent: str
    estimated_word_count: int

    headings: list[str]
    pain_points: list[str]
    key_topics: list[str]
    practical_advice: list[str]
    faq_topics: list[str]

    strengths: list[str]
    weaknesses: list[str]
    potential_gaps: list[str]
```

竞争文章全文只提供给：

```text
CompetitorAnalyzer
```

**不得直接把五篇全文全部交给 ArticleWriter。**

---

# 17. SERP 综合分析

输入：

```text
5 x CompetitorAnalysis
PAA
Related Searches
Keyword Context
```

输出：

```python
class SERPSynthesis(BaseModel):
    dominant_intent: str
    secondary_intents: list[str]

    common_topics: list[str]
    common_pain_points: list[str]
    common_questions: list[str]

    content_patterns: list[str]
    missing_topics: list[str]
    opportunities: list[str]
```

---

# 18. Evidence Research

SEO competitor sources 与 factual evidence 必须分开。

竞争文章用于：

```text
Search Intent
SERP Structure
Pain Point
FAQ
Content Gap
```

不能因为竞争对手说：

```text
"studies show..."
```

就当作可信研究来源。

Evidence Research 负责：

```text
心理学理论
研究结果
统计数据
行为机制
潜意识 / manifestation 的科学边界
```

输出：

```python
class EvidenceNote(BaseModel):
    claim: str
    source_title: str
    source_url: str
    source_type: str
    confidence: Literal["high", "medium", "low"]
    usage: Literal["supported", "soften", "avoid"]
    note: str | None = None
```

Writer 只能使用 Evidence Notes 中存在的具体研究/数字。

禁止自己编造统计数字和论文。

---

# 19. Keyword Dataset

系统允许两种模式。

## 19.1 Dataset Mode

存在 SEMrush Excel：

```text
Keyword
Volume
KD
CPC
Intent
Sheet
```

系统可执行：

```text
High Volume
Low KD
High CPC
Long Tail
```

策略选择。

---

## 19.2 SERP-only Mode

如果用户直接输入热门词条，但数据库中找不到对应 Keyword Dataset：

```text
keyword_metrics_available = false
```

仍可生产文章。

但不得让 LLM 猜：

```text
Volume
KD
CPC
```

UI 明确显示：

```text
Keyword metrics unavailable
```

---

# 20. Excel Import

建议支持：

```text
.xlsx
```

每个 Sheet：

```text
= Topic Cluster
```

导入时先做列名映射。

可接受别名：

```text
Keyword / keyword
Volume / Search Volume
KD / Keyword Difficulty / KD %
CPC
Intent
```

不能识别的列：

```text
warning
```

不能静默猜测。

---

# 21. Internal Link Dataset

内链规则单独标准化：

```python
class InternalLinkRule(BaseModel):
    marker: str
    anchor_text: str | None
    target_url: str
    topic: str | None
    keywords: list[str]
    active: bool
```

文章生成阶段：

```text
LLM 只能插 marker
```

例如：

```text
[[INTERNAL_LINK:ATTACHMENT_TEST]]
```

不能自己写 URL。

最终 Renderer：

```text
marker
↓
validate
↓
resolve
↓
Markdown link
```

---

# 22. Content Brief Schema

```python
class ContentBrief(BaseModel):
    primary_keyword: str
    secondary_keywords: list[str]
    long_tail_keywords: list[str]

    search_intent: str
    search_stage: str
    article_strategy: str

    target_reader: str
    core_problem: str
    emotional_context: str

    unique_angle: str
    content_gaps: list[str]

    required_topics: list[str]
    faq_questions: list[str]

    target_function: str | None
    cta_strategy: list[str]

    internal_link_markers: list[str]

    recommended_word_count: int
```

---

# 23. Outline Schema

不要让 Outline 只返回 Markdown 字符串。

```python
class OutlineSection(BaseModel):
    heading: str
    level: Literal[2, 3]
    purpose: str
    keywords: list[str] = []
    cta_slot: bool = False


class ArticleOutline(BaseModel):
    title: str
    sections: list[OutlineSection]
    faq_questions: list[str]
```

程序验证：

```text
一个 title
H2/H3 层级合法
有 Practical 部分
有 FAQ
有 CTA Slot
覆盖 required topics
```

失败：

```text
Outline Repair
```

最多 2 次。

---

# 24. Article Domain Model

```python
class ArticleDocument(BaseModel):
    title: str

    body_markdown: str

    seo_title: str
    meta_description: str
    slug: str

    primary_keyword: str
    secondary_keywords: list[str]
    long_tail_keywords: list[str]

    search_intent: str
    article_strategy: str

    target_function: str | None
```

注意：

```text
ArticleDocument.body_markdown
```

永远不含 H1。

---

# 25. Article Writer 输入

Writer 只能收到：

```text
SEO Guideline
Content Brief
Validated Outline
SERP Synthesis
Evidence Notes
Allowed Internal Link Markers
```

不收到：

```text
5 篇完整 competitor source
```

这样降低：

- 抄袭风险；
- context 占用；
- 模仿竞品结构过度；
- 本地模型注意力稀释。

---

# 26. Reviewer Pipeline

文章至少经过：

```text
SEO Reviewer
Fact Reviewer
Human Style Reviewer
```

三个 Reviewer 只输出问题，不直接修改文章。

---

## 26.1 SEO Review

```python
class SEOReview(BaseModel):
    total_score: int
    keyword_score: int
    search_intent_score: int
    structure_score: int
    readability_score: int
    cta_score: int

    issues: list[str]
    required_changes: list[str]
```

---

## 26.2 Fact Review

```python
class FactIssue(BaseModel):
    quote_or_claim: str
    verdict: Literal[
        "supported",
        "soften",
        "remove",
        "needs_source"
    ]
    reason: str


class FactReview(BaseModel):
    issues: list[FactIssue]
```

重点扫描：

```text
statistics
scientific studies
attachment theory
dopamine
cortisol
nervous system
diagnosis
treatment
manifestation
subliminal
```

---

## 26.3 Human Style Review

```python
class StyleReview(BaseModel):
    score: int
    ai_patterns: list[str]
    repetitive_patterns: list[str]
    weak_sections: list[str]
    required_changes: list[str]
```

---

# 27. Article Revision

统一输入：

```text
Original Draft
SEO Review
Fact Review
Style Review
```

由：

```text
ArticleReviser
```

生成一个新版本。

不得覆盖原始 Draft。

---

# 28. Article Versioning

数据库必须保存：

```text
v1 writer
v2 revision
v3 manual regeneration
...
```

结构：

```text
article_versions
```

字段：

```text
id
job_id
version
stage
title
body_markdown
seo_title
meta_description
slug
model
prompt_name
prompt_version
created_at
```

---

# 29. Anti-copy Check

V1 使用程序检查：

1. 分句；
2. 与 5 个 competitor sources 比较；
3. RapidFuzz / SequenceMatcher；
4. 连续长片段匹配标记。

建议条件：

```text
>= 12 words exact/near exact
```

或：

```text
similarity >= 0.85
```

标记：

```text
possible_source_overlap
```

进入 Reviser。

不是简单通过“查重率”决定通过/失败。

---

# 30. Image Planner

图片规划发生在：

```text
Final Article Revision 完成之后
```

因为必须根据最终文章内容决定图数和插入位置。

Schema：

```python
class ImagePlanItem(BaseModel):
    role: Literal["hero", "inline"]
    purpose: str

    section_heading: str | None
    insertion_marker: str | None

    filename: str
    alt_text: str
    prompt: str

    aspect_ratio: str


class ImagePlan(BaseModel):
    total_count: int
    images: list[ImagePlanItem]
```

约束：

```text
1 <= total_count <= 3
images[0].role == "hero"
只有 hero 可以没有 insertion_marker
inline 必须存在 insertion_marker
```

---

# 31. Image Count Service

图片数量先由程序决定 ceiling：

```python
def suggested_image_count(word_count: int) -> int:
    if word_count < 1200:
        return 1
    if word_count <= 2200:
        return 2
    return 3
```

Image Planner 可以：

```text
减少
```

不能：

```text
增加超过 ceiling
```

总上限仍然：

```text
IMAGE_MAX_COUNT=3
```

---

# 32. Hero Image Prompt

Hero 的 Prompt 必须来自：

```text
文章主题
目标读者
核心情绪
独特角度
品牌视觉规范
```

不要简单：

```text
"illustration about anxious attachment"
```

Image Planner 应描述：

```text
subject
scene
mood
composition
camera / illustration style
lighting
negative constraints
```

后续新增：

```text
prompts/brand_visual_guideline.md
```

即可统一品牌视觉，而不用修改 Python。

---

# 33. Inline Image Marker

Article Writer 不直接决定图片 URL。

Image Planner 选择插入段落后，在最终内部 body 加：

```text
[[IMAGE:inline-1]]
[[IMAGE:inline-2]]
```

Image Generation 完成后 Renderer 替换成：

```markdown
![Alt text](STRAPI_MEDIA_URL)
```

Hero 不使用 marker。

---

# 34. 图片本地结构

```text
data/
└── articles/
    └── {job_uuid}/
        ├── article.md
        ├── article.json
        ├── content-brief.json
        ├── outline.json
        ├── serp.json
        ├── review.json
        ├── sources.json
        └── images/
            ├── hero.webp
            ├── inline-1.webp
            └── inline-2.webp
```

---

# 35. Strapi V1 API 规则

本规范默认 Strapi 5。

如果实际服务器是 Strapi 4：

```text
只修改 StrapiCMSProvider
```

不得修改 Article Pipeline。

---

## 35.1 Draft 安全规则

创建：

```text
POST /api/{pluralApiId}?status=draft
```

更新：

```text
PUT /api/{pluralApiId}/{documentId}?status=draft
```

任何 Strapi Blog 写请求都必须显式：

```text
status=draft
```

代码中不能依赖默认行为。

CMSProvider V1 不允许实现 publish 方法。

---

# 36. Strapi Draft Sync 推荐顺序

由于 `mainImage` 是 Media 字段，同时正文图片需要 Strapi Media URL，V1 采用两阶段 Draft Sync：

```text
STEP A
创建 text-only Draft
↓
获得 blog id + documentId

STEP B
上传 Hero，并直接关联 mainImage

STEP C
上传 inline images，获得 media URLs

STEP D
将 [[IMAGE:*]] 替换为 Strapi Media URLs

STEP E
PUT Draft
写入最终 body
再次显式 ?status=draft

STEP F
GET Draft 验证字段
```

这样：

- 不依赖不稳定的 media `connect` 写法；
- Hero 可以使用 Strapi Upload API 的 entry linking；
- inline image 可以先上传获取真实 URL；
- 重试时可以更新同一个 Draft。

---

# 37. Strapi 创建 Draft Payload

首次创建：

```http
POST /api/blogs?status=draft
Authorization: Bearer ${STRAPI_API_TOKEN}
Content-Type: application/json
```

示意：

```json
{
  "data": {
    "title": "Why Anxious Attachment Makes No Contact So Hard",
    "slug": "anxious-attachment-no-contact",

    "body": "Temporary body without resolved image URLs...",

    "metaTitle": "Anxious Attachment and No Contact",
    "metaDescription": "Understand why no contact can feel so intense...",
    "seoKeywords": "anxious attachment no contact, attachment anxiety",

    "author": "AUTHOR_DOCUMENT_ID",
    "category": "CATEGORY_DOCUMENT_ID"
  }
}
```

`author` / `category` 是 many-to-one 时优先使用 Strapi 5 的短格式 documentId。

如果真实服务器行为与此不一致，只在：

```text
StrapiCMSProvider relation serializer
```

中修复，并补 integration test。

---

# 38. Hero 上传并绑定 mainImage

在 Draft 已创建后：

```http
POST /api/upload
Authorization: Bearer ...
Content-Type: multipart/form-data
```

Form Data：

```text
files     = hero.webp
ref       = api::blog.blog
refId     = <blog numeric id>
field     = mainImage
fileInfo  = {
  "name": "...",
  "alternativeText": "...",
  "caption": "..."
}
```

必须使用实际 Blog UID：

```env
STRAPI_BLOG_UID=api::blog.blog
```

不要在 Python 代码里硬编码。

---

# 39. Inline Image Upload

Inline image 不需要绑定到单独 Media relation。

直接：

```http
POST /api/upload
```

获得：

```text
id
url
alternativeText
```

然后：

```text
[[IMAGE:inline-1]]
```

替换为：

```markdown
![ALT](PUBLIC_STRAPI_MEDIA_URL)
```

如果 Strapi 返回：

```text
/uploads/xxx.webp
```

使用：

```text
STRAPI_BASE_URL
+
relative URL
```

生成公开绝对 URL。

---

# 40. Final Draft Update

```http
PUT /api/blogs/{documentId}?status=draft
```

Payload：

```json
{
  "data": {
    "body": "Final markdown with resolved inline image URLs..."
  }
}
```

不得省略：

```text
?status=draft
```

---

# 41. Strapi Sync Idempotency

表：

```text
strapi_syncs
```

必须保存：

```text
job_id
strapi_id
strapi_document_id
sync_status
last_payload_hash
```

如果用户重复点击：

```text
Push Draft to Strapi
```

系统判断已有：

```text
strapi_document_id
```

则：

```text
UPDATE existing draft
```

不得创建第二篇重复 Draft。

---

# 42. Strapi Slug Collision

同步前：

```text
GET /api/blogs?filters[slug][$eq]={slug}&status=draft
GET /api/blogs?filters[slug][$eq]={slug}&status=published
```

如果已有：

### 同一 Job 的 documentId

更新。

### 其他文章

返回：

```text
SLUG_CONFLICT
```

不自动追加：

```text
-2
-3
```

避免错误覆盖 SEO URL。

UI 让用户决定：

```text
修改 slug
或
取消同步
```

---

# 43. Web 页面

V1 只做必要页面。

---

## 43.1 `/`

New Article。

字段：

```text
Target Keyword *
Language
Market
Target Function

Strategy:
  Auto
  High Volume
  Low KD
  High CPC
  Long Tail
  Pillar

Author
Category

Image Mode:
  Auto (recommended)
  1
  2
  3

[Generate]
```

Image Mode 即使手工指定也必须：

```text
<= 3
```

---

## 43.2 `/jobs`

显示：

```text
Keyword
Status
Progress
Model
Created At
Actions
```

---

## 43.3 `/jobs/{id}`

分区：

```text
Pipeline
Keyword
SERP
Sources
Content Brief
Outline
Article
Reviews
Images
Strapi
Logs
```

HTMX：

```text
每 2–3 秒更新状态
```

---

## 43.4 `/articles/{job_id}`

Preview：

```text
Title
Hero
Rendered Markdown
SEO metadata
Internal links
```

---

## 43.5 Strapi Section

状态：

```text
Not synced
Syncing
Draft created
Sync failed
```

按钮：

```text
[Push Draft to Strapi]
[Update Existing Draft]
[Open in Strapi]
```

不存在：

```text
Publish
```

按钮。

---

# 44. Internal REST API

```text
POST   /api/jobs
GET    /api/jobs
GET    /api/jobs/{id}

POST   /api/jobs/{id}/retry
POST   /api/jobs/{id}/cancel

GET    /api/jobs/{id}/article
GET    /api/jobs/{id}/serp
GET    /api/jobs/{id}/sources
GET    /api/jobs/{id}/reviews

POST   /api/jobs/{id}/sync-strapi

GET    /api/providers/status

POST   /api/datasets/keywords/import
GET    /api/datasets/keywords

GET    /api/strapi/authors
GET    /api/strapi/categories
```

---

# 45. Create Job Contract

Request：

```json
{
  "keyword": "anxious attachment no contact",
  "language": "en",
  "market": "US",
  "target_function": "attachment_test",
  "strategy": "auto",

  "author_document_id": null,
  "category_document_id": null,

  "image_count_override": null
}
```

Response：

```json
{
  "job_id": "uuid",
  "status": "queued"
}
```

---

# 46. 数据库表

至少需要以下表。

---

## 46.1 generation_jobs

```text
id UUID PK
keyword TEXT NOT NULL

language VARCHAR
market VARCHAR
target_function VARCHAR
strategy VARCHAR

author_document_id VARCHAR NULL
category_document_id VARCHAR NULL

image_count_override INT NULL

status VARCHAR NOT NULL
current_step VARCHAR NULL

keyword_metrics_available BOOLEAN

error_code VARCHAR NULL
error_message TEXT NULL

created_at TIMESTAMPTZ
started_at TIMESTAMPTZ
completed_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
```

---

## 46.2 keyword_clusters

```text
id UUID PK
name TEXT
sheet_name TEXT
strapi_category_document_id VARCHAR NULL
created_at
```

---

## 46.3 keywords

```text
id UUID PK
cluster_id FK

keyword TEXT
volume INT NULL
kd NUMERIC NULL
cpc NUMERIC NULL
intent VARCHAR NULL

source VARCHAR
created_at

UNIQUE(cluster_id, keyword)
```

---

## 46.4 internal_link_rules

```text
id UUID PK

marker TEXT UNIQUE
anchor_text TEXT NULL
target_url TEXT
topic TEXT NULL
keywords JSONB
active BOOLEAN

created_at
updated_at
```

---

## 46.5 serp_runs

```text
id UUID PK
job_id FK

provider VARCHAR
query TEXT

location_code INT
language_code VARCHAR
device VARCHAR

raw_response JSONB
provider_cost NUMERIC NULL

created_at
```

---

## 46.6 serp_results

```text
id UUID PK
serp_run_id FK

result_type VARCHAR
rank INT NULL

title TEXT NULL
url TEXT NULL
normalized_url TEXT NULL
domain TEXT NULL
snippet TEXT NULL

raw_item JSONB
```

---

## 46.7 source_pages

```text
id UUID PK

url TEXT
normalized_url TEXT
url_hash CHAR(64) UNIQUE

title TEXT NULL
domain TEXT

content_markdown TEXT
content_hash CHAR(64)

extractor VARCHAR

first_seen_at
last_fetched_at
```

---

## 46.8 job_sources

```text
job_id FK
source_page_id FK

serp_rank INT
source_role VARCHAR

PRIMARY KEY(job_id, source_page_id)
```

---

## 46.9 competitor_analyses

```text
id UUID PK
job_id FK
source_page_id FK

analysis JSONB

model VARCHAR
prompt_version VARCHAR

created_at
```

---

## 46.10 evidence_notes

```text
id UUID PK
job_id FK

claim TEXT
source_title TEXT
source_url TEXT
source_type VARCHAR
confidence VARCHAR
usage VARCHAR
note TEXT NULL

created_at
```

---

## 46.11 content_briefs

```text
id UUID PK
job_id FK
brief JSONB
model VARCHAR
prompt_version VARCHAR
created_at
```

---

## 46.12 article_outlines

```text
id UUID PK
job_id FK
outline JSONB
model VARCHAR
prompt_version VARCHAR
created_at
```

---

## 46.13 article_versions

```text
id UUID PK
job_id FK

version INT
stage VARCHAR

title TEXT
body_markdown TEXT

seo_title TEXT
meta_description TEXT
slug TEXT

model VARCHAR
prompt_name VARCHAR
prompt_version VARCHAR

created_at

UNIQUE(job_id, version)
```

---

## 46.14 article_reviews

```text
id UUID PK
job_id FK
article_version_id FK

review_type VARCHAR
review JSONB

model VARCHAR
prompt_version VARCHAR

created_at
```

---

## 46.15 images

```text
id UUID PK
job_id FK

role VARCHAR
sort_order INT

purpose TEXT
section_heading TEXT NULL
insertion_marker TEXT NULL

prompt TEXT
filename TEXT
alt_text TEXT
aspect_ratio VARCHAR

local_path TEXT
mime_type VARCHAR

provider VARCHAR
provider_request_id TEXT NULL

strapi_media_id INT NULL
strapi_media_document_id VARCHAR NULL
strapi_url TEXT NULL

created_at
```

---

## 46.16 strapi_syncs

```text
id UUID PK
job_id FK UNIQUE

strapi_id INT NULL
strapi_document_id VARCHAR NULL

sync_status VARCHAR

last_payload JSONB NULL
last_payload_hash CHAR(64) NULL

error_message TEXT NULL

created_at
updated_at
```

---

# 47. Prompt 文件

全部 Prompt 外置：

```text
prompts/
├── seo_article_guideline.md
├── competitor_analyzer.md
├── serp_synthesis.md
├── evidence_research.md
├── content_brief.md
├── outline_generator.md
├── outline_repair.md
├── article_writer.md
├── seo_reviewer.md
├── fact_reviewer.md
├── style_reviewer.md
├── article_reviser.md
├── image_planner.md
└── brand_visual_guideline.md
```

Python 文件禁止出现一整套长 Prompt。

---

# 48. Prompt Version

每个 Prompt 文件顶部：

```yaml
---
name: article_writer
version: 1.0
---
```

加载时计算：

```text
SHA256(prompt content)
```

记录：

```text
prompt_name
prompt_version
prompt_hash
```

---

# 49. Structured Output 容错

本地模型必须假设可能输出非法 JSON。

统一实现：

```text
generate_structured()
```

流程：

```text
LLM Call
↓
extract JSON
↓
json.loads
↓
Pydantic validation
↓
success -> return
↓
failure
↓
JSON Repair Prompt
↓
retry
```

最多：

```text
2 次 repair
```

仍失败：

```text
LLM_STRUCTURED_OUTPUT_INVALID
```

保存 raw output 方便 Debug。

---

# 50. LLM Context 管理

每一步严格限制输入。

### Competitor Analyzer

```text
SEO requirement excerpt
Single competitor page
Keyword
```

### SERP Synthesis

```text
5 x structured competitor analysis
PAA
Related Search
```

### Writer

```text
SEO Guideline
Content Brief
Outline
SERP Synthesis
Evidence Notes
Internal Link Rules
```

### Reviewer

```text
SEO Guideline
Brief
Article
```

### Image Planner

```text
Title
Article summary / final article
Brand Visual Guideline
Image count ceiling
```

禁止：

```text
每一步都把所有历史 Prompt + 所有网页全文塞进去
```

---

# 51. Retry Policy

统一 HTTP Retry：

可重试：

```text
429
500
502
503
504
Timeout
Connection Reset
```

不可自动重试：

```text
400 validation error
401
403
404 configuration error
```

退避：

```text
2s
5s
15s
```

最多 3 次。

LLM：

```text
2 retry
```

Image：

```text
2 retry
```

Strapi：

```text
2 retry
```

---

# 52. Error Codes

不要只存 Exception String。

至少定义：

```text
DATAFORSEO_AUTH_FAILED
DATAFORSEO_REQUEST_FAILED
DATAFORSEO_EMPTY_SERP

EXTRACTOR_AUTH_FAILED
EXTRACTOR_FAILED
SOURCE_EMPTY

LLM_UNAVAILABLE
LLM_STRUCTURED_OUTPUT_INVALID
LLM_CONTEXT_OVERFLOW

ARTICLE_VALIDATION_FAILED

IMAGE_PROVIDER_FAILED
IMAGE_PLAN_INVALID

STRAPI_AUTH_FAILED
STRAPI_SCHEMA_MISMATCH
STRAPI_SLUG_CONFLICT
STRAPI_UPLOAD_FAILED
STRAPI_DRAFT_CREATE_FAILED
STRAPI_DRAFT_UPDATE_FAILED
```

---

# 53. Logging

JSON structured logging。

每条至少：

```text
timestamp
level
job_id
step
provider
event
duration_ms
error_code
```

绝不记录：

```text
API Key
Authorization Header
DataForSEO password
Strapi Token
```

---

# 54. Cost Tracking

即使第一版不做财务面板，也保留：

```text
provider_cost
```

用于：

```text
DataForSEO
Exa/Tavily
Image Generation
```

LLM 是本地部署时可以记录：

```text
input_tokens
output_tokens
duration
```

方便以后分析生成成本与性能。

---

# 55. 项目目录

```text
seo-auto/
│
├── app/
│   ├── main.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── enums.py
│   │   ├── exceptions.py
│   │   └── logging.py
│   │
│   ├── db/
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models/
│   │       ├── job.py
│   │       ├── keyword.py
│   │       ├── serp.py
│   │       ├── source.py
│   │       ├── article.py
│   │       ├── image.py
│   │       └── strapi_sync.py
│   │
│   ├── schemas/
│   │   ├── jobs.py
│   │   ├── serp.py
│   │   ├── sources.py
│   │   ├── research.py
│   │   ├── article.py
│   │   ├── images.py
│   │   └── strapi.py
│   │
│   ├── providers/
│   │   ├── llm/
│   │   │   ├── base.py
│   │   │   └── openai_compatible.py
│   │   ├── serp/
│   │   │   ├── base.py
│   │   │   └── dataforseo.py
│   │   ├── extractor/
│   │   │   ├── base.py
│   │   │   ├── exa.py
│   │   │   └── tavily.py
│   │   ├── image/
│   │   │   ├── base.py
│   │   │   └── openai_image.py
│   │   └── cms/
│   │       ├── base.py
│   │       └── strapi.py
│   │
│   ├── services/
│   │   ├── prompt_service.py
│   │   ├── url_normalizer.py
│   │   ├── source_cache.py
│   │   ├── slug_service.py
│   │   ├── keyword_service.py
│   │   ├── keyword_import.py
│   │   ├── internal_link_service.py
│   │   ├── article_renderer.py
│   │   ├── image_count_service.py
│   │   └── artifact_service.py
│   │
│   ├── pipeline/
│   │   ├── orchestrator.py
│   │   └── steps/
│   │       ├── keyword_prepare.py
│   │       ├── serp_search.py
│   │       ├── source_extract.py
│   │       ├── competitor_analysis.py
│   │       ├── serp_synthesis.py
│   │       ├── evidence_research.py
│   │       ├── content_brief.py
│   │       ├── outline.py
│   │       ├── article_writer.py
│   │       ├── seo_review.py
│   │       ├── fact_review.py
│   │       ├── style_review.py
│   │       ├── article_reviser.py
│   │       ├── image_plan.py
│   │       └── image_generate.py
│   │
│   ├── workers/
│   │   └── article_worker.py
│   │
│   ├── routes/
│   │   ├── web.py
│   │   ├── jobs.py
│   │   ├── datasets.py
│   │   ├── providers.py
│   │   └── strapi.py
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── new_job.html
│   │   ├── jobs.html
│   │   ├── job_detail.html
│   │   └── article_preview.html
│   │
│   └── static/
│
├── prompts/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── migrations/
├── data/
│
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
└── SEO-AUTO-DEV-SPEC.md
```

---

# 56. Docker Compose

四个服务：

```text
web
worker
postgres
redis
```

llama.cpp：

```text
外部服务
```

Strapi：

```text
外部服务
```

示意：

```yaml
services:

  web:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8080
    env_file:
      - .env
    depends_on:
      - postgres
      - redis

  worker:
    build: .
    command: python -m app.workers.article_worker
    env_file:
      - .env
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:17
    environment:
      POSTGRES_DB: seo
      POSTGRES_USER: seo
      POSTGRES_PASSWORD: seo

  redis:
    image: redis:7-alpine
```

---

# 57. Provider Health Check

页面：

```text
/settings
```

展示：

```text
LLM           ✓ Connected
DataForSEO    ✓ Connected
Exa           ✓ Connected
Image API     ✓ Connected
Strapi        ✓ Connected
PostgreSQL    ✓ Connected
Redis         ✓ Connected
```

只显示：

```text
Configured
Missing
Connected
Failed
```

不得回显 Secret。

---

# 58. 开发阶段总览

不要一次要求 Qwen3.8-27B 实现全部项目。

采用：

```text
P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P8 -> P9
```

每个阶段都必须可以单独运行和验收。

---

# P0 — Skeleton / Infrastructure

## 目标

搭建可以启动的工程骨架。

## 实现

```text
FastAPI
Config
SQLAlchemy
PostgreSQL
Alembic
Redis
RQ
Docker Compose
Logging
Health Endpoint
```

## 主要文件

```text
app/main.py
app/core/config.py
app/core/logging.py
app/db/base.py
app/db/session.py
docker-compose.yml
Dockerfile
.env.example
pyproject.toml
```

## API

```text
GET /health
```

输出：

```json
{
  "status": "ok",
  "database": true,
  "redis": true
}
```

## 验收

```text
docker compose up -d
```

后：

```text
GET /health -> 200
Postgres -> connected
Redis -> connected
Alembic migration -> works
pytest -> pass
```

不要实现 LLM / SERP。

---

# P1 — Core Models + Provider Framework

## 目标

固定 Domain Model 与 Provider Interface。

## 实现

```text
JobStatus
LLMProvider
SERPProvider
ContentExtractor
ImageProvider
CMSProvider

GenerationJob
ArticleDocument
SERP schemas
```

实现：

```text
OpenAICompatibleLLMProvider
```

## 验收

通过配置的 llama.cpp：

```text
"Return hello"
```

可以成功得到结果。

再测试：

```text
structured output -> Pydantic model
```

非法 JSON 可以自动 repair。

---

# P2 — DataForSEO + Source Extraction

## 目标

输入关键词获得：

```text
Google SERP
PAA
Top competitor URLs
Extracted content
```

## 实现

```text
DataForSEOSERPProvider
ExaContentExtractor
URLNormalizer
SourceCache
SERP DB tables
Source DB tables
```

## 验收关键词

```text
anxious attachment no contact
```

结果：

```text
DataForSEO 请求成功
至少保存 organic results
选出 Top 5 unique competitor URLs
保存 PAA（若 SERP 有）
抓取 competitor content
```

第二次运行同一 URL：

```text
有效 TTL 内 -> cache hit
```

不重新调用 Extractor。

---

# P3 — Keyword Dataset + Internal Links

## 目标

把之前 SEO 规范依赖的 SEMrush Excel 纳入系统。

## 实现

```text
Excel Import
Keyword Cluster
Keyword Metrics
Internal Link Rules
```

## 验收

上传一个包含多个 Sheet 的 Excel：

```text
Sheet -> cluster
Keyword -> row
Volume/KD/CPC -> parsed
```

重复导入：

```text
update/upsert
```

不制造重复记录。

输入一个不在 Dataset 的 Keyword：

```text
仍可创建 Job
keyword_metrics_available=false
```

---

# P4 — Research Pipeline

## 目标

从原始竞品内容变成结构化研究资料。

## 实现

```text
CompetitorAnalyzer
SERPSynthesis
EvidenceResearch
ContentBriefGenerator
OutlineGenerator
OutlineValidator
```

## 验收

Job Detail 能看到：

```text
SERP
5 competitor analyses
SERP synthesis
Evidence notes
Content brief
Outline
```

所有 LLM 中间结果均：

```text
Pydantic validated
DB persisted
```

---

# P5 — Article Generation + QA

## 目标

生成符合《SEO 文章 AI 生产规范》的最终文章。

## 实现

```text
ArticleWriter
SEOReviewer
FactReviewer
StyleReviewer
ArticleReviser
Anti-copy Check
Article Versioning
Markdown Renderer
```

## 验收

最终：

```text
ArticleDocument.title
ArticleDocument.body_markdown
seo_title
meta_description
slug
keywords
```

同时：

```text
body_markdown 没有 # H1
```

本地导出的：

```text
article.md
```

只有一个 H1。

必须保存：

```text
Writer version
Revision version
Reviews
```

---

# P6 — Image Pipeline

## 目标

动态生成 1–3 张图。

## 实现

```text
ImageCountService
ImagePlanner
OpenAIImageProvider
Image DB model
Local storage
Image marker
```

## 验收

### 1000 words

```text
1 image
hero only
```

### 1800 words

```text
2 images
hero + inline
```

### 2600 words

```text
3 images
hero + inline + inline
```

任何情况下：

```text
<= 3
```

Hero：

```text
不出现在 body 中
```

Inline：

```text
通过 [[IMAGE:*]] marker 放置
```

---

# P7 — Strapi Integration

## 目标

把当前 Blog Schema 正确创建成 Draft。

## 先做 Schema Discovery

启动时不能只假设 endpoint 一定是：

```text
/api/blogs
```

配置：

```env
STRAPI_BLOG_PLURAL_API_ID=blogs
STRAPI_BLOG_UID=api::blog.blog
```

Health Check：

```text
GET /api/{pluralApiId}?pagination[pageSize]=1
```

并确认 Token 权限。

## 实现

```text
StrapiCMSProvider
List Authors
List Categories
Create Draft
Update Draft
Upload Hero
Upload Inline Media
Resolve Media URL
Slug Conflict
Idempotent Sync
```

## Draft Flow

```text
Create Draft ?status=draft
↓
Upload hero linked to mainImage
↓
Upload inline media
↓
Resolve image markers
↓
Update Draft ?status=draft
↓
Verify
```

## 验收

Strapi Admin 中：

```text
Blog 存在
Status = Draft
Title correct
Slug correct
Author correct
Category correct
mainImage correct
Body Markdown correct
metaTitle correct
metaDescription correct
seoKeywords correct
```

系统不能提供 Publish。

---

# P8 — Web UI

## 目标

让整个系统不依赖 CLI。

## 页面

```text
New Article
Job List
Job Detail
Article Preview
Keyword Dataset
Provider Status
```

## 验收

用户可以：

1. 输入 Keyword；
2. Generate；
3. 浏览器关闭；
4. Worker 继续运行；
5. 回到 Job 页面查看结果；
6. Preview；
7. 点击 Push Draft；
8. 在 Strapi 人工审核。

---

# P9 — Reliability / Production Hardening

## 实现

```text
Retry by step
Resume from checkpoint
Force source refresh
Provider timeout
Provider fallback
Better error UI
Anti-copy tuning
Cost tracking
Prompt version dashboard
Job cancellation
Cleanup
Backup
```

可选实现：

```text
Tavily extractor fallback
```

逻辑：

```text
Exa failed
+
TAVILY_API_KEY configured
↓
Tavily Extract
```

---

# 59. Testing Strategy

测试必须分：

```text
unit
integration
```

---

## 59.1 Unit Tests

至少：

```text
test_url_normalizer.py
test_slug_service.py
test_image_count_service.py
test_article_renderer.py
test_internal_link_service.py
test_dataforseo_parser.py
test_structured_output_parser.py
test_strapi_payload.py
```

---

## 59.2 Integration Tests

外部 API 不应每次 pytest 都真实调用。

使用 fixtures / mocked HTTP。

另外允许：

```text
RUN_EXTERNAL_INTEGRATION_TESTS=true
```

时执行真实：

```text
llama.cpp
DataForSEO
Exa
Strapi
```

测试。

---

## 59.3 Strapi 最重要 Integration Test

使用专用测试 slug：

```text
seo-auto-integration-test-{uuid}
```

流程：

```text
Create draft
Upload small test image
Attach mainImage
Update body
GET draft
Verify status
Delete test draft
```

**绝不测试 Publish。**

---

# 60. Security

必须：

```text
.env 加入 .gitignore
API Token 不进日志
Web 页面不显示 secret
HTTP error 不 dump Authorization header
```

Strapi API Token：

```text
Custom Token
```

只开放：

```text
Blog read/create/update
Author read
Category read
Upload
```

系统不需要：

```text
Delete Blog
Publish
Admin APIs
```

如果 Strapi 权限体系允许，应不给自动化 Token 删除/发布权限。

---

# 61. Qwen3.8-27B Coding Agent 固定开发指令

每次开发任务都附加：

```text
你正在实现 SEO-AUTO-DEV-SPEC.md 的一个明确阶段。

规则：

1. 先阅读 SEO-AUTO-DEV-SPEC.md。
2. 只实现本次指定 Phase / Task。
3. 不提前实现后续 Phase。
4. 不修改已经冻结的 Provider Interface，除非本次任务明确要求。
5. 所有 URL、API Key、Model Name、timeout 均从 Settings/.env 获取。
6. 所有 HTTP 使用 httpx。
7. 不在业务代码中写长 Prompt，Prompt 必须位于 prompts/。
8. LLM structured output 必须经过 Pydantic validation。
9. 每一个 Pipeline Step 完成后必须持久化 checkpoint。
10. 不允许把整个 Pipeline 状态只放内存。
11. 数据库结构改变必须创建 Alembic migration。
12. 新增核心逻辑必须增加 pytest。
13. 不允许使用 TODO 或 mock 替代本阶段要求的核心功能。
14. 不要为了兼容已有错误实现而破坏规范架构。
15. 如果规范与现有代码冲突，先报告冲突，不自行改变规范。
16. 完成后输出：
    - 修改文件列表
    - 实现说明
    - Migration
    - 测试方法
    - 测试结果
    - 未解决问题
```

---

# 62. 每次给 Coding Agent 的任务大小

建议：

```text
一次 1 个 Provider
或
一次 1–2 个 Pipeline Steps
或
5–15 个紧密相关文件
```

不要：

```text
“按照这个文档把整个项目做完”
```

例如正确任务：

```text
实现 P2-A：
DataForSEOSERPProvider + schemas + parser + mocked tests。
不要实现 Exa。
```

完成后再：

```text
P2-B：
实现 URLNormalizer + SourceCache。
```

再：

```text
P2-C：
实现 ExaContentExtractor。
```

---

# 63. Definition of Done — 单篇文章

一篇文章只有满足以下全部条件才进入：

```text
READY
```

### SERP

- [ ] DataForSEO 请求成功
- [ ] 保存 Raw Response
- [ ] 获得 organic results
- [ ] 获取最多 5 个有效 competitor sources
- [ ] URL 去重完成
- [ ] Source Cache 工作正常

### Research

- [ ] Competitor Analysis 完成
- [ ] SERP Synthesis 完成
- [ ] Evidence Notes 完成
- [ ] Content Brief Pydantic valid
- [ ] Outline valid

### Article

- [ ] 只有一个逻辑 H1
- [ ] `body_markdown` 无 H1
- [ ] SEO Title
- [ ] Meta Description
- [ ] Slug
- [ ] FAQ
- [ ] CTA
- [ ] Internal Link Marker valid
- [ ] Fact Review 完成
- [ ] Style Review 完成
- [ ] Anti-copy 无严重问题

### Images

- [ ] 1–3 张
- [ ] 第 1 张是 Hero
- [ ] Hero 不重复写入正文
- [ ] Inline 图插入合理位置
- [ ] Alt Text 存在
- [ ] 文件名规范

只有满足以上要求：

```text
JobStatus = READY
```

---

# 64. Definition of Done — Strapi Draft

只有以下全部成功：

- [ ] `title`
- [ ] `slug`
- [ ] `author`
- [ ] `category`
- [ ] `mainImage`
- [ ] `body`
- [ ] `metaDescription`
- [ ] `metaTitle`
- [ ] `seoKeywords`
- [ ] GET 验证 Draft
- [ ] `strapi_document_id` 已保存

才标记：

```text
STRAPI_DRAFT_CREATED
```

如果图片上传到一半失败：

```text
STRAPI_SYNC_FAILED
```

但保留已有：

```text
documentId
```

重试时继续更新该 Draft，不创建新 Draft。

---

# 65. V1 明确不做

```text
自动 Publish
自动删除 Strapi Article
自动修改线上已 Published Article
Google Search Console 排名跟踪
自动刷新旧文章
多用户权限体系
收费系统
自动社交媒体分发
复杂 Agent 自主决策
Vector RAG
自动评论
```

---

# 66. V1 之后的建议演进

完成 V1 后可增加：

## V1.1

```text
Tavily fallback
Multiple target markets
Brand Image Style
Keyword cluster dashboard
Category auto mapping
```

## V1.2

```text
Google Search Console
Ranking Monitor
Existing Content Cannibalization Detection
Article Refresh Suggestions
```

## V2

```text
Bulk Article Queue
Content Calendar
Scheduled Draft Production
CTR Title Optimization
Automated Update Workflow
Human Review Workflow
```

仍建议保持：

```text
自动生成 Draft
人工 Publish
```

---

# 67. 当前实现优先级

现在建议严格按以下顺序：

```text
P0  Skeleton
↓
P1  Provider Interface + llama.cpp
↓
P2  DataForSEO + Exa + Cache
↓
P3  SEMrush Excel / Internal Links
↓
P4  SEO Research
↓
P5  Article + QA
↓
P6  GPT-Image-2
↓
P7  Strapi Draft
↓
P8  Complete Web UI
↓
P9  Reliability
```

其中第一阶段真正可以验证“AI SEO 内容质量”的节点是：

```text
P5
```

真正形成端到端生产系统的节点是：

```text
P8
```

---

# 68. 外部 API 参考

## DataForSEO

Google Organic Live Advanced:

```text
https://api.dataforseo.com/v3/serp/google/organic/live/advanced
```

Documentation:

```text
https://docs.dataforseo.com/v3/serp-se-type-live-advanced/
```

V1 使用：

```text
depth=10
location_code=2840
language_code=en
device=desktop
calculate_rectangles=false
load_async_ai_overview=false
```

---

## Strapi 5 REST

REST API:

```text
https://docs.strapi.io/cms/api/rest
```

Upload API:

```text
https://docs.strapi.io/cms/api/rest/upload
```

Relations:

```text
https://docs.strapi.io/cms/api/rest/relations
```

V1 核心安全要求：

```text
Create  -> ?status=draft
Update  -> ?status=draft
```

---

# 69. 最终工程原则

整个项目最重要的边界是：

```text
Search
≠
Extract
≠
Analyze
≠
Write
≠
Review
≠
Render
≠
Publish
```

系统应该是一个：

```text
可观察
可重试
可替换 Provider
可保存中间结果
人工最终发布
```

的确定性内容生产 Pipeline。

不要把它实现成：

```text
一个 Prompt
+
一堆 Tool Calling
+
让 Agent 自己决定下一步
```

V1 的核心目标不是“Agent 看起来很智能”，而是：

```text
文章质量稳定
过程可追踪
失败可恢复
来源可审计
Strapi 永远只创建 Draft
```
