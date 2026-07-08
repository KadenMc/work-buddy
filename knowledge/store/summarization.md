---
name: Summarization
kind: system
description: Producer + search surface for content summaries. Per-session TL;DR+topics, per-page flat extracts, and the coarse-to-fine retrieval funnel over them.
tags:
- allow-transient-labels
- summarization
- search
- progressive-disclosure
aliases:
- summary system
- summary search
- framework summaries
---

Top-level system unit for the summarization framework's user-facing surface. The framework itself (composer + axes + storage) is documented at `architecture/summarization-framework`. The unified agent workflow (find → walk → read) lives at `disclosure/`. This unit describes which producers feed the summary store, which consumers read from it, and how to use them together.

## Producers

Summaries are produced by registered framework compositions, not by direct callers:

- `conversation_session` composition — conv_obs's sidecar cron (`conversation-observability-summarize.md`) drives it every 2 hours. Each session → a `SummaryNode` tree (root TLDR + ~5-8 topic children carrying span pointers). See `conversation_observability/conversation_observability_summarize`.
- `chrome_page` composition — driven by `chrome_infer._summarize_tabs` during Chrome triage. Flat (depth-1) trees stored in the LLM cache with a 30-minute TTL.

## Consumers / search

Three capabilities consume summaries; pick by the question you're answering.

- **`summary_search(query, scope=?)`** — coarse-to-fine retrieval funnel. Stage 1 ranks summary nodes; stage 2 (optional, default on) drills the top items via `session_search`. Returns `stage1_hits` + `candidate_items` + `drilled`. Each hit carries a `drill_node_id` ready to hand to `drill_tree`. **Use this when you have a topic and don't know which item.**
- **`drill_tree(domain="summary", node_id=..., depth=...)`** — walk one specific item's tree at three depths. **Use this when you have an item id and want its structure.** See `disclosure/drill_tree`.
- **`conversation_observability_summary_get(session_id)`** — legacy direct row read (flat dict with `tldr`, `topics: [...]`). Consumed by the dashboard `/api/chats/<id>/topics` endpoint and the `claude_session_summary` context collector. **Prefer `drill_tree` for new code**; this surface stays until consumers migrate (tracked as task `t-bbefceef`).

## Agent workflow

The canonical pattern when finding a past session by topic:

```
# Topic in hand, drill on by default
result = summary_search(query="...", scope="conversation_session")

# Already-drilled raw turns are in result['drilled'][item_id]['hits']
# For deeper exploration of one candidate:
outline = drill_tree(
    domain="summary",
    node_id=result['candidate_items'][0]['drill_node_id'],
    depth="summary",
)

# To read sequentially around a specific turn:
turns = session_expand(session_id=..., message_index=N, span=5)
```

For non-summary topic searches (raw turn text, Chrome tabs, etc.) reach for `context_search` instead. The full decision table is documented at `disclosure/`.

## IR indexing

A single IR source (`summary`, in `work_buddy/ir/sources/summary.py`) reads from the durable summary store at `<data_root>/summarization/summarization.db` and emits one Document per node. Build with `ir_index(source="summary")`; the `summary-index-rebuild` sidecar job rebuilds every 5 minutes.

**Only durable-backed compositions are indexed.** The IR source reads from `summary_items` + `summary_nodes` (the `DurableSummaryStore` backend). Compositions using `TtlCacheStore` (today: `chrome_page`) keep their results in the LLM cache file, not the durable DB — they are NOT searchable via `summary_search` and NOT walkable via `drill_tree(domain="summary", ...)`. This is by design: TTL-backed entries expire (Chrome page summaries: 30 min) and search over evictable entries returns inconsistent results. A composition that wants to be searchable picks `DurableSummaryStore`.

New durable-backed domains become searchable + drillable as soon as they ship a composition; no per-domain IR source or navigator to write.
