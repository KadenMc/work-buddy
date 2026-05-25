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

## IR indexing

A single IR source (`summary`, in `work_buddy/ir/sources/summary.py`) reads the framework's `summary_nodes` table and emits one Document per node. Build with `ir_index(source="summary")`. New summarizable domains — add a composition to the framework and they're automatically indexable; no per-domain IR source code.
