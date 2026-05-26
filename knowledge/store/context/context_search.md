---
name: Context Search
kind: capability
description: 'Search indexed content (conversations, documents, tabs). Requires IR index — build with ir_index first. Methods: ''substring'' (exact match, no embedding service), ''keyword'' (BM25), ''semantic'' (dense), or comma-delimited combo like ''keyword,semantic'' (default, RRF fused).'
capability_name: context_search
category: context
op: op.wb.context_search
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: Search query
    required: true
  top_k:
    type: int
    description: Max results (default 10)
    required: false
  source:
    type: str
    description: 'Filter by source type (e.g. ''conversation''). Default: all sources.'
    required: false
  scope:
    type: str
    description: Narrow to a specific item within a source (e.g. a session_id for conversations, a tab_id for Chrome tabs). Uses doc_id prefix matching.
    required: false
  method:
    type: str
    description: Search method(s). 'substring' (exact, no service needed), 'keyword' (BM25), 'semantic' (dense), or comma-delimited like 'keyword,semantic' (default). substring is solo-only.
    required: false
  recency:
    type: bool
    description: Apply recency bias to favor recent results (default true). Set false to rank purely by text relevance.
    required: false
tags:
- context
- search
aliases:
- find conversation
- search sessions
- which session had
- conversation about
- look up chat
- search index
- information retrieval
parents:
- context
---

Universal IR search across every indexed source. Builds the BM25 + dense vectors at index time (`ir_index(source=...)`); `context_search(query, source=..., method=...)` ranks documents at query time. Returns markdown-formatted results.

## When to use `context_search` vs related capabilities

- Use **`context_search`** when you want to rank by query across one or more raw indexed sources and don't need post-hit drilling. Common path: `context_search(query, source="conversation")` for raw turn text across sessions; `context_search(query, source="chrome")` for tab text; `context_search(query, source="task_note")` for task notes; `context_search(query, source="docs")` for documents. Omitting `source` searches everywhere (results dilute fast — prefer a `source` filter when you know which domain).
- Use **`summary_search`** when the topic lives in the summarization-framework `summary` source AND you want the per-namespace drill stage. `summary_search` is `context_search(source="summary")` plus a registered drill handler that calls `session_search` per top item, returned as `drilled` in the structured response. See `summarization/summary_search`.
- Use **`drill_tree`** when you already have a specific node id (e.g. from a `summary_search` hit) and want to walk its tree structure. No ranking involved. See `disclosure/drill_tree`.

Agent workflow shape (`find → walk → read`) is documented in `disclosure/`.

## Method selection

- `method="keyword,semantic"` (default) — RRF-fuses BM25 + dense. Strongest for natural-language queries.
- `method="keyword"` — BM25 only. Good for queries with strong vocabulary overlap; cheaper, doesn't need the embedding service for the dense leg.
- `method="semantic"` — dense only. Good for paraphrased / conceptual queries.
- `method="substring"` — exact-string match. Use for file paths, identifiers, error strings. Cannot combine with other methods.

## Scope

`scope=` is a doc-id **prefix** filter. The doc-id format is source-specific; common patterns:

- `conversation` source: `scope="{session_id}"` filters to one session's turns.
- `summary` source: `scope="{namespace}:"` filters to one summary namespace (e.g. `"conversation_session:"`).
- `task_note` source: `scope="task_note:{task_id}"` filters to one task's note.

## Recency bias

The default ranking applies a half-life-14d recency multiplier (configured under `ir.recency` in `config.yaml`). For multi-month-old targets, pass `recency=false` to rank purely by text relevance — the default tends to bury older hits even when their content is the better match.

## Requires the IR index

Run `ir_index(action="build", source=...)` if a source's index is cold. The `summary`, `conversation`, and `task_note` sources are kept fresh by sidecar crons (`summary-index-rebuild`, `ir-index-rebuild`, `task-note-index`).
