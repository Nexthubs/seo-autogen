"""Article generation pipeline (SEO-AUTO-DEV-SPEC.md sections 9, 55).

P2 implements the first two steps (serp_search, source_extract).
Steps follow the checkpoint principle (spec section 9): every step
persists its results, updates the job status and commits before the
next step runs — no pipeline state lives only in memory.
"""
