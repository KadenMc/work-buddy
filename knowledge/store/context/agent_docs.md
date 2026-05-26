---
name: Agent Docs
kind: capability
description: 'Search and navigate all agent documentation: directions, system docs, capabilities, and workflows. Supports exact path lookup, subtree browsing, and natural language search with hierarchical progressive disclosure.'
capability_name: agent_docs
category: context
op: op.wb.agent_docs
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: Natural language search. Empty + no path/scope = full index.
    required: false
  path:
    type: str
    description: Exact unit path for direct lookup (e.g. 'journal/running-notes', 'tasks/triage')
    required: false
  scope:
    type: str
    description: Path prefix to filter to a subtree (e.g. 'tasks/', 'obsidian/')
    required: false
  kind:
    type: str
    description: 'Filter by kind: directions, capability, workflow, system, service, integration, reference, concept'
    required: false
  depth:
    type: str
    description: 'Content depth: ''index'' (navigation), ''summary'' (default), ''full'' (complete)'
    required: false
  top_n:
    type: int
    description: Max search results (default 8)
    required: false
  dev:
    type: bool
    description: Include dev_notes in full-depth results. Auto-set when session dev mode is active.
    required: false
  recursive:
    type: str
    description: Placeholder recursion at depth='full'. 'default' (per-placeholder --recursive flag wins), 'all' (force transitive expansion, capped at ~100KB), 'none' (preserve <<wb:...>> markup literally; useful for editing). Affects output only — search corpus uses 'default'.
    required: false
  max_depth:
    type: int
    description: Cap on placeholder recursion depth at depth='full'. -1 (default) = mode default (unlimited in 'default' mode, 10 in 'all' mode). 0 = no recursion (same as recursive='none' in effect). Positive ints set an exact cap. Layers with the size budget and the per-unit-occurrence cap.
    required: false
tags:
- context
- agent
- docs
aliases:
- documentation
- knowledge
- docs
- how does
- what is
- help
- guide
- reference
- manual
- agent docs
- self documentation
- how to
- find capability
- what can I do
parents:
- context
---

## Structured-result alternative — `find(source="docs")`

`agent_docs` returns prose-formatted results with the full unit lookup conveniences (path matching, depth filters, recursive `<<wb:...>>` expansion). For *structured* (dict-shaped) results — e.g. when chaining into [`walk`](../disclosure/walk) for full-content navigation, or building UI lists — reach for [`find`](../search/find)`(source="docs", query="...")` instead. Same backing data (the unified knowledge store); BM25 + dense ranking; structured hit list.

The `docs` IR source is kept fresh by the `docs-index-rebuild` sidecar job (every 15 minutes). Both `agent_docs` and `find(source="docs")` read from the same store; the difference is in the return shape and dispatch behaviour.
