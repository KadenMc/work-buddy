---
name: Progressive Disclosure
kind: system
description: Unified navigation contract for tree-shaped drillable resources. One MCP capability (drill_tree) walks any registered TreeDrillable at three depths (index/summary/full).
tags:
- disclosure
- drill
- navigation
- progressive-disclosure
- tree-walk
aliases:
- drill into resource
- tree navigation
- progressive disclosure surface
- unified drill
- depth navigation
---

Cross-cutting navigation contract for resources that have a tree shape. A `TreeDrillable` Protocol exposes one method (`get(node_id, depth)`) returning a `TreeView`; the registry maps domain names to `TreeDrillable` instances; the dispatch capability `drill_tree(domain, node_id, depth)` routes the call.

Three depths:
- `index` — this node + child names only. Cheapest. Used when you only need to know what's available.
- `summary` — this node + each child's summary text. Right for triage: which child matters? without paying for full content.
- `full` — this node + everything. The agent has the actual material.

## Registered domains today

### `summary` — framework summaries

Wraps `summarization.db` (`summary_items` + `summary_nodes`). Three node-id shapes:

- `{namespace}` (no colon) — the namespace itself. Children are every summary item under that namespace, ordered by `generated_at` DESC. Use for discovery ("show me every summarized session").
- `{namespace}:{item_id}` — the whole item (root of one summarized session or page). Children are the level-1 topic nodes.
- `{namespace}:{item_id}#n{ordinal}` — a specific node within the tree.

The IR `summary` source's `doc_id` field uses `{namespace}:{item_id}:n{ordinal}`; a `summary_search` hit can be drilled by swapping the final `:n` for `#n`.

### `knowledge` — knowledge units

Wraps the knowledge store via `agent_docs`. node_id is the unit path (`tasks/triage-directions`, `architecture/summarization-framework`, etc.). The `roots` are the top-level domain directories; `agent_docs(scope=...)` is what you'd use for cross-cutting search, but `drill_tree` gives a uniform walk-by-id surface.

## Out of scope (today)

Sequence-shaped resources (session transcripts, workflow step logs) and field-keyed resources (`context_drill_down`'s task notes / git diffs / project descriptions) keep their existing per-domain capabilities. `TreeDrillable` is deliberately tree-shaped — forcing those into the same Protocol would shape the abstraction around accidents of which one was tested first. They wrap opportunistically when their owners next touch them.

## Adding a new domain

1. Implement a class with `domain: str` and `get(node_id, depth) -> TreeView` (raise `DrillError` on bad input).
2. Register via `register_drillable(domain, factory)` in your subsystem's module-level init (or in `disclosure/registry.py:_register_defaults` if it's a built-in).
3. Document the node_id format in your domain's knowledge unit. Consumers use `drill_tree(your_domain, ...)` immediately.

## Key files

- `work_buddy/disclosure/protocol.py` — `TreeDrillable` Protocol, `TreeView`, `ChildRef`, `DrillError`.
- `work_buddy/disclosure/registry.py` — per-domain registry + default registrations.
- `work_buddy/disclosure/summary_tree.py` — `SummaryTreeDrillable` (summary domain).
- `work_buddy/disclosure/knowledge_tree.py` — `KnowledgeTreeDrillable` (knowledge domain).
- `work_buddy/disclosure/dispatch.py` — `drill_tree(domain, node_id, depth)` entry point.
- `work_buddy/mcp_server/ops/disclosure_ops.py` — MCP op binding.
