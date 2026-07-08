---
name: Summary Search
kind: capability
description: Coarse-to-fine retrieval funnel over framework summaries. Stage 1 ranks query against summary nodes; stage 2 (optional) drills into raw spans of top items. Each hit carries a `drill_node_id` ready to hand to `drill_tree`.
capability_name: summary_search
category: summarization
parameters:
  query:
    type: str
    description: Natural-language query.
    required: true
  scope:
    type: str
    description: Restrict stage 1 to one summary namespace (e.g. 'conversation_session'). Omit to search all summary namespaces. Named `scope` to match `context_search` / `agent_docs` vocabulary.
    required: false
  top_k:
    type: int
    description: Stage-1 cap — how many summary nodes to consider (default 8).
    required: false
  drill:
    type: bool
    description: When true, run stage 2 over top candidates (default true).
    required: false
  drill_top_k:
    type: int
    description: How many distinct items to drill (default 5).
    required: false
  drill_per_item_top_k:
    type: int
    description: How many raw-span hits per drilled item (default 5).
    required: false
  method:
    type: str
    description: 'Search method: ''keyword'', ''semantic'', or ''keyword,semantic'' (default).'
    required: false
op: op.wb.summary_search
schema_version: wb-capability/v1
tags:
- allow-transient-labels
- summarization
- search
- retrieval
- funnel
- progressive-disclosure
aliases:
- search summaries
- find session by topic
- summary funnel
- session topic search
- drill into session
parents:
- summarization
---

Coarse-to-fine retrieval funnel over the IR `summary` index.

`summary_search` is the `source="summary"` form of [`find`](../search/find) — both return the same funnel-shape dict and both default `drill=False` (rank-first; pass `drill=True` to inline raw spans). `find` is the universal verb; `summary_search` is the back-compat alias maintained indefinitely.

**When to reach for this vs. other tools**:

- Use **`summary_search`** when you have a *topic* and don't know which item — it ranks across the compressed layer (TLDRs + topic titles/summaries/keywords) and optionally drills the top items into their raw sources.
- Use **`drill_tree`** when you already have a *node id* and want to walk its structure at a chosen depth (`index` / `summary` / `full`). See `disclosure/drill_tree`.
- Use **`context_search`** when you want a general IR search across any source (`conversation`, `chrome`, `task_note`, `docs`, `projects`, `summary`) and DON'T need the funnel's drill stage. `summary_search` builds on `context_search`'s underlying engine and adds the per-namespace drill.

The handoff from `summary_search` to `drill_tree` is literal: every `stage1_hits` entry includes a `drill_node_id` field (`{namespace}:{item_id}` for root nodes, `{namespace}:{item_id}#n{ordinal}` for internal nodes) that drops straight into `drill_tree(domain="summary", node_id=...)`. Same for `candidate_items` (item-root drill coordinate).
