---
name: Find
kind: capability
description: Structured IR search across any indexed source. Returns a plain list of hits, or — when `drill=True` — the funnel shape (`stage1_hits` + `candidate_items` + `drilled`). Subsumes `summary_search` (which remains as an alias).
capability_name: find
category: search
parameters:
  query:
    type: str
    description: Natural-language query string.
    required: true
  source:
    type: str
    description: 'Filter by source name: ''conversation'', ''summary'', ''chrome'', ''task_note'', ''docs'', ''projects''. Omit for cross-source search.'
    required: false
  scope:
    type: str
    description: 'Doc-id prefix filter (source-specific). conversation: a session_id. summary: a namespace (the funnel appends the trailing '':'' automatically).'
    required: false
  drill:
    type: bool
    description: When True, run a per-source drill handler against the top items. Returns the funnel shape `{stage1_hits, candidate_items, drilled}`. Default False (returns the plain IR hit list).
    required: false
  top_k:
    type: int
    description: Stage-1 result cap (default 10).
    required: false
  method:
    type: str
    description: '''keyword'', ''semantic'', ''keyword,semantic'' (default), or ''substring''. ''substring'' is solo-only.'
    required: false
  recency:
    type: bool
    description: Apply recency bias (default per config; pass False for time-insensitive ranking).
    required: false
  drill_top_k:
    type: int
    description: When drilling, how many distinct items to drill (default 5).
    required: false
  drill_per_item_top_k:
    type: int
    description: When drilling, how many raw-span hits per item (default 5).
    required: false
op: op.wb.find
schema_version: wb-capability/v1
tags:
- allow-transient-labels
- search
- ir
- retrieval
- find
- progressive-disclosure
aliases:
- search anything
- find content
- universal search
- ir search structured
parents:
- search
---

Universal structured IR search. Two return modes:

- **`drill=False`** (default) — returns `list[dict]` mirroring `ir.search.search`'s raw output. Each hit has `doc_id`, `score`, `source`, `display_text`, `metadata`. Use this when you just want ranked hits.
- **`drill=True`** — returns the funnel-shape dict: `{query, scope, stage1_hits, candidate_items, drilled, error?}`. `stage1_hits` are per-node hits with `drill_node_id` ready to hand to `walk` / `drill_tree`. `candidate_items` are per-item aggregates (best score across hits in the same item). `drilled` maps `item_id` to the per-source drill handler's output (e.g., for `source="summary"` and namespace `conversation_session`, this is a `session_search` result).

## When to reach for this vs. related tools

- Use **`find`** when downstream code needs the structured hits — chaining into `walk` / `drill_tree`, computing aggregates, building UI lists, etc.
- Use **`context_search`** when you want markdown-formatted output for human eyeballs. Same underlying engine.
- Use **`summary_search`** for back-compat code that depends on the funnel-shape return; it's the `source="summary"` form of `find` and, like `find`, defaults `drill=False` (pass `drill=True` for the raw-span drill).
- Use **`walk`** (or **`drill_tree`**) when you already have a node id and want to navigate by id, not by query.

## Sources with drill handlers today

- **`summary`** — drill dispatches by namespace (`conversation_session` → `session_search`). Other namespaces return `None`; the funnel still emits the coarse hits.

Sources without a registered drill handler get an empty `drilled` block and a DEBUG log on `drill=True`. Register a new handler via `work_buddy.summarization.drill_registry.register_drill_handler(source, handler)`.

## Searching the knowledge store

`find(source="docs", query="...")` ranks knowledge-store units (`.md` files under `knowledge/store/`). The `docs` IR source is the structured-result alternative to `agent_docs(query=...)` — same backing data, BM25+dense ranking, plus the cross-source composability that `find` provides. Use `agent_docs(query=...)` when you want the prose-formatted results with the full unit lookup conveniences (path matching, depth filters, recursive expansion); use `find(source="docs")` when you need the structured hit list (e.g., chaining into `walk(domain="knowledge", node_id=hit_path)` for full-content navigation).

## Requires the IR index

Run `ir_index(action="build", source=...)` if a source's index is cold. The `summary`, `conversation`, `task_note`, and `docs` sources are kept fresh by sidecar crons (`summary-index-rebuild`, `ir-index-rebuild`, `task-note-index`, `docs-index-rebuild`).
