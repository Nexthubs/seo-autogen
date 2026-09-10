# Coding Agent Instructions

## Source of Truth

本项目开发必须遵循以下两份文档：

1. `SEO-AUTO-DEV-SPEC.md`
   - 工程架构与开发规范
   - 最高优先级

2. `prompts/seo_article_guideline.md`
   - SEO 内容生产规则
   - 仅用于内容生成 Pipeline

如果代码、历史实现或其他文档与
`SEO-AUTO-DEV-SPEC.md` 冲突，
以 `SEO-AUTO-DEV-SPEC.md` 为准。

## Development Rule

不要一次实现整个项目。

严格按照：
P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P8 -> P9

逐阶段执行。

每次只实现用户明确指定的 Phase / Task。
禁止提前实现后续 Phase。
