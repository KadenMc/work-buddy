---
name: Drill Tree
kind: capability
description: 'Walk a tree-shaped drillable resource at three depths (index|summary|full). Default depth is `index` — cheapest walk. Today''s domains: knowledge (units via agent_docs), summary (summarization framework''s per-node store).'
capability_name: drill_tree
category: disclosure
parameters:
  domain:
    type: str
    description: 'Registered domain name. Today: ''knowledge'' or ''summary''. Use available_domains() / inspect the disclosure system unit to see all registered.'
    required: true
  node_id:
    type: str
    description: 'Domain-specific node identifier. knowledge: unit path (e.g. ''architecture/summarization-framework''). summary: ''{namespace}:{item_id}'' for the whole tree or ''{namespace}:{item_id}#n{ordinal}'' for an internal node.'
    required: true
  depth:
    type: str
    description: '''index'' (default; node + child names only — cheapest), ''summary'' (node + each child''s summary text), or ''full'' (everything).'
    required: false
op: op.wb.drill_tree
schema_version: wb-capability/v1
tags:
- allow-transient-labels
- disclosure
- drill
- navigation
- progressive-disclosure
- tree-walk
aliases:
- drill
- drill into
- walk tree
- navigate resource
- progressive disclosure
parents:
- disclosure
---

Universal tree navigation across registered `TreeDrillable` domains.

`drill_tree` and [`walk`](walk) are the same op — `walk` is the canonical short name. Both are stable; use whichever reads naturally in context.

**When to reach for this vs. other tools**:

- Use **`drill_tree`** when you already have a *node id* and want to navigate its structure. No ranking is performed; the response is the requested view at the chosen depth.
- Use **`summary_search`** when you have a *topic / query* and need to find which items match it. See `summarization/summary_search`. `summary_search`'s stage-1 hits surface a `drill_node_id` field that drops straight into `drill_tree(domain="summary", ...)` with no string translation.
- Use **`context_search`** for general IR search across any registered source.

## Depths

- `index` (default) — this node + child names only. Cheapest. Use for catalog browsing or when you only need to know what's available.
- `summary` — this node + each child's summary text. Use for triage ("which child matters?") without paying for full content.
- `full` — everything: this node's full content + the subtree concat for tree domains.

Default is `index` rather than `summary` so an agent reaching for `drill_tree(node_id="{namespace}")` gets a cheap catalog list rather than triggering N per-child summary fetches; opt into deeper views explicitly.

## Domains today

### `summary` — framework summaries

Node-id shapes:
- `{namespace}` (no colon) — namespace root. Children are every summary item under that namespace, ordered by `generated_at` DESC.
- `{namespace}:{item_id}` — the whole item (root of one summarized session or page). Children are level-1 topic nodes.
- `{namespace}:{item_id}#n{ordinal}` — a specific node within the tree.

### `knowledge` — knowledge units

Node_id is the unit path (`tasks/triage-directions`, `architecture/summarization-framework`, etc.). Wraps `agent_docs(path=, depth=)` so the same walk-by-id verb covers the knowledge store and the summary store.

## Adding a new domain

Implement a class with `domain: str` and `get(node_id, depth) -> TreeView` (raise `DrillError` on bad input), then register via `register_drillable(domain, factory)`. New tree-shaped domains plug in immediately — no per-domain capability to write.
