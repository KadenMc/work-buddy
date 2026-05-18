---
name: Context Search
kind: capability
description: 'Search indexed content (conversations, documents, tabs). Requires IR index — build with ir_index first. Methods: ''substring'' (exact match, no embedding service), ''keyword'' (BM25), ''semantic'' (dense), or comma-delimited combo like ''keyword,semantic'' (default, RRF fused).'
capability_name: context_search
category: context
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
- context
---
