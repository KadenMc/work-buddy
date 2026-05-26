---
name: Walk
kind: capability
description: 'Universal tree navigation — canonical short name for `drill_tree`. Walks any registered `TreeDrillable` at three depths (index | summary | full). Today''s domains: knowledge (units), summary (framework per-node store).'
capability_name: walk
category: disclosure
parameters:
  domain:
    type: str
    description: 'Registered domain name. Today: ''knowledge'' or ''summary''.'
    required: true
  node_id:
    type: str
    description: 'Domain-specific node identifier. knowledge: unit path (e.g. ''architecture/summarization-framework''). summary: ''{namespace}:{item_id}'' for the whole tree or ''{namespace}:{item_id}#n{ordinal}'' for an internal node.'
    required: true
  depth:
    type: str
    description: '''index'' (default; node + child names only — cheapest), ''summary'' (node + each child''s summary text), or ''full'' (everything).'
    required: false
op: op.wb.walk
schema_version: wb-capability/v1
tags:
- disclosure
- drill
- navigation
- progressive-disclosure
- tree-walk
- walk
aliases:
- navigate tree
- walk tree
- drill into resource
- progressive disclosure walk
parents:
- disclosure
---

`walk` is the canonical short name for [`drill_tree`](drill_tree). Both bind the same underlying op (`drill_tree_op`); the agent-facing verb is `walk`. Use whichever name reads more naturally in context — both are stable and supported indefinitely.

## When to reach for this vs. related tools

- Use **`walk`** when you already have a *node id* and want to navigate its structure at a chosen depth.
- Use **`find(source=..., scope=..., drill=True)`** (or its alias `summary_search`) when you have a *topic* and want to *search* for matching nodes across an indexed source. The structured `find` response includes a `drill_node_id` ready to hand to `walk`.
- Use **`context_search`** for human-eyeball markdown ranking.

## Depths

- `index` (default) — this node + child names only. Cheapest. Catalog browsing.
- `summary` — this node + each child's summary text. Triage.
- `full` — everything: this node's full content + the subtree concat for tree domains.

## Domains today

Same as `drill_tree`:
- **`summary`** — framework summaries (`{namespace}:{item_id}` / `{namespace}:{item_id}#n{ordinal}`).
- **`knowledge`** — knowledge units (unit path, e.g. `architecture/summarization-framework`).

New tree-shaped domains plug in via `work_buddy.disclosure.registry.register_drillable` without needing a new capability declaration — they're discovered through the same `walk` verb.
