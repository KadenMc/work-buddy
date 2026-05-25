---
name: Summary Search
kind: capability
description: Coarse-to-fine retrieval funnel over framework summaries. Stage 1 ranks query against summary nodes; stage 2 (optional) drills into raw spans of top items.
capability_name: summary_search
category: summarization
parameters:
  query:
    type: str
    description: Natural-language query.
    required: true
  namespace:
    type: str
    description: Restrict stage 1 to one summary namespace (e.g. 'conversation_session'). Omit to search all summary namespaces.
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
