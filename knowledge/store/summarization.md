---
name: Summarization
kind: system
description: Producer + search surface for content summaries. Per-session TL;DR+topics, per-page flat extracts, and the coarse-to-fine retrieval funnel over them.
tags:
- summarization
- search
- progressive-disclosure
aliases:
- summary system
- summary search
- framework summaries
---

Top-level system unit for the summarization framework's user-facing surface. The framework itself (composer + axes + storage) is documented at `architecture/summarization-framework`. This unit lists the MCP capabilities that produce or consume summaries.

## Producers

Summaries are produced by registered framework compositions, not by direct callers:

- `conversation_session` composition — conv_obs's sidecar cron drives it. See `conversation_observability/conversation_observability_summarize`.
- `chrome_page` composition — driven by `chrome_infer._summarize_tabs` during Chrome triage.

## Consumers / search

- `summary_search` — coarse-to-fine retrieval funnel. Stage 1 ranks the query against summary nodes (compressed layer); stage 2 (optional) drills into raw spans of top-ranked items.
- `conversation_observability_summary_get` — direct row lookup by session id (consumed by the dashboard and `claude_session_summary` collector).
- `drill_tree(domain="summary", ...)` — the unified navigation contract walks the per-node tree at three depths. See `disclosure/`.

## IR indexing

A single IR source (`summary`, in `work_buddy/ir/sources/summary.py`) reads from the durable summary store at `<data_root>/summarization/summarization.db` and emits one Document per node. Build with `ir_index(source="summary")`; the `summary-index-rebuild` sidecar job rebuilds every 5 minutes.

**Only durable-backed compositions are indexed.** The IR source reads from `summary_items` + `summary_nodes` (the `DurableSummaryStore` backend). Compositions using `TtlCacheStore` (today: `chrome_page`) keep their results in the LLM cache file, not the durable DB — they are NOT searchable via `summary_search` and NOT walkable via `drill_tree(domain="summary", ...)`. This is by design: TTL-backed entries expire (Chrome page summaries: 30 min) and search over evictable entries returns inconsistent results. A composition that wants to be searchable picks `DurableSummaryStore`.

New durable-backed domains become searchable + drillable as soon as they ship a composition; no per-domain IR source or navigator to write.
