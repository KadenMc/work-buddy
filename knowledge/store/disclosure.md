---
name: Progressive Disclosure
kind: system
description: Unified navigation contract for tree-shaped drillable resources. One MCP capability (drill_tree) walks any registered TreeDrillable at three depths (index/summary/full).
tags:
- allow-transient-labels
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

Cross-cutting navigation contract for resources that have a tree shape, **plus the agent-facing decision rule** for which verb to reach for when. A `TreeDrillable` Protocol exposes one method (`get(node_id, depth)`) returning a `TreeView`; the registry maps domain names to `TreeDrillable` instances; the dispatch capability `drill_tree(domain, node_id, depth)` routes the call.

## Three depths

- `index` (default) — this node + child names only. Cheapest. Use when you only need to know what's available.
- `summary` — this node + each child's summary text. Right for triage ("which child matters?") without paying for full content.
- `full` — this node + everything. The agent has the actual material in hand.

## Agent workflows

The full system has three verbs: **search by topic**, **walk by id**, **read sequentially**. They compose into one flow.

### Find → walk → read

When you have a topic and want to land on the right turns:

1. **Find by topic** — `summary_search(query, scope="conversation_session")` ranks summary nodes (the compressed layer: TLDRs + topic titles + keywords). Each hit carries a pre-built `drill_node_id` field that pairs directly with `drill_tree`. The funnel returns only this compact ranking by default (`drill=False`); pass `drill=True` to also inline the top items' raw spans via `session_search` — opt into that once you've picked a candidate, since drilling the whole top-N can produce oversized payloads.
2. **Walk by id** — if the funnel's stage-2 drill missed nuance or you want the whole topic outline for a candidate session, `drill_tree(domain="summary", node_id="conversation_session:<sid>", depth="summary")` returns the full tldr + every topic with its summary in one cheap read. For deeper exploration of one topic: `drill_tree(..., node_id="...#n<ordinal>", depth="full")`.
3. **Read sequentially** — once the agent has a session id and a turn range from the drill stage, `session_get(session_id, offset, limit)` browses raw turns and `session_expand(session_id, message_index, span=5)` zooms around a specific turn.

### Walk by id alone (no query)

When you already know which item:

- `drill_tree(domain="summary", node_id="conversation_session")` — every summarized session listed with its TLDR (set `depth="summary"`).
- `drill_tree(domain="summary", node_id="conversation_session:<sid>", depth="summary")` — one session's topic outline.
- `drill_tree(domain="knowledge", node_id="tasks/triage-directions", depth="full")` — the full content of one knowledge unit.

### Searching across non-summary sources

`summary_search` is `context_search(source="summary")` plus a per-namespace drill stage. For everything else — raw conversation spans, Chrome tabs, task notes, documents, projects — use `context_search` directly:

- `context_search(query, source="conversation")` — raw turn text across all sessions. Fallback when summaries don't cover a recent / error / exact-substring case.
- `context_search(query, source="chrome")` — currently-open and recently-engaged Chrome tab text.
- `context_search(query, source="task_note")` — task-linked markdown notes.
- `context_search(query, source="docs")` — indexed documents.

### Decision table

| You have... | Reach for |
|---|---|
| A topic, want which sessions match | `summary_search(query, scope="conversation_session")` |
| A topic, want any matching content from any source | `context_search(query)` (omit `source`) |
| A specific item id (session, knowledge unit, etc.) | `drill_tree(domain=..., node_id=..., depth=...)` |
| A session id, want raw turn text by offset | `session_get(session_id, offset, limit)` |
| A session id + turn index, want surrounding turns | `session_expand(session_id, message_index, span=5)` |
| A search hit's `span_index`, want the message index | `session_locate(session_id, span_index)` |
| A `summary_search` hit, want to keep drilling | Hand `hit['drill_node_id']` to `drill_tree(domain="summary", node_id=hit['drill_node_id'], depth=...)` |

The `drill_node_id` field on every `summary_search` hit is the literal handoff coordinate — no string translation between the IR layer's `doc_id` format (`{ns}:{id}:n{ord}`) and the disclosure layer's `node_id` format (`{ns}:{id}#n{ord}`). The funnel emits the disclosure-format string ready to paste.

## Registered domains today

### `summary` — framework summaries

Wraps `summarization.db` (`summary_items` + `summary_nodes`). Three node-id shapes:

- `{namespace}` (no colon) — the namespace itself. Children are every summary item under that namespace, ordered by `generated_at` DESC. Use for discovery ("show me every summarized session").
- `{namespace}:{item_id}` — the whole item (root of one summarized session or page). Children are the level-1 topic nodes.
- `{namespace}:{item_id}#n{ordinal}` — a specific node within the tree.

The IR `summary` source's `doc_id` field uses `{namespace}:{item_id}:n{ordinal}` (the IR convention is uniform `:` separators); a `summary_search` hit's `drill_node_id` field already encodes the disclosure-format equivalent so direct translation isn't needed.

### `knowledge` — knowledge units

Wraps the knowledge store via `agent_docs`. node_id is the unit path (`tasks/triage-directions`, `architecture/summarization-framework`, etc.). The `roots` are the top-level domain directories. `agent_docs(scope=...)` is the cross-cutting search form; `drill_tree` gives a uniform walk-by-id surface that pairs symmetrically with the `summary` domain.

## Out of scope (today)

Sequence-shaped resources (session transcripts, workflow step logs) and field-keyed resources (`context_drill_down`'s task notes / git diffs / project descriptions) keep their existing per-domain capabilities. `TreeDrillable` is deliberately tree-shaped — forcing those into the same Protocol would shape the abstraction around accidents of which one was tested first. They wrap opportunistically when their owners next touch them.

The deferred follow-up (task `t-bbefceef`) consolidates these into universal `find` / `walk` verbs across the whole search/navigate surface; until then, the per-domain capabilities listed above are the canonical entries.

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
