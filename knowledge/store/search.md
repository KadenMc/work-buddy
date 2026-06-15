---
name: Search
kind: concept
description: Universal IR search verb (`find`) plus the markdown-formatted twin (`context_search`). Search any indexed source — conversations, summaries, knowledge units, Chrome tabs, task notes, documents, projects — with optional per-source drill.
scope: system
tags:
- search
- ir
- retrieval
- find
aliases:
- universal search
- find anything
- ir surface
parents: []
---

Search surface for work-buddy. Two ops at the agent-facing layer:

- **`find`** — structured-returning universal verb. Returns a `list[dict]` for plain ranking, or the funnel shape (`stage1_hits` + `candidate_items` + `drilled`) when `drill=True`. Use when downstream code needs to *act on* the structured hits (chain into `drill_tree`, aggregate per-item, etc.).
- **`context_search`** — markdown-formatted twin. Same underlying IR engine; emits human-readable markdown. Use for human-eyeball reads.

Both delegate to `work_buddy.ir.search.search` for the actual ranking. Sources are pluggable: `conversation`, `summary`, `chrome`, `task_note`, `docs`, `projects`. A source with a registered drill handler (see `work_buddy.summarization.drill_registry`) participates in the `drill=True` per-item drill stage; sources without one get an empty `drilled` block.

## Children

- [find](search/find) — structured search op
- [walk](search/walk) — universal tree navigation alias (see also [disclosure/drill_tree](disclosure/drill_tree))

## Related

- [context_search](context/context_search) — markdown twin
- [summary_search](summarization/summary_search) — pre-find compatible alias, the `find(source="summary")` funnel (defaults `drill=False`)
- [drill_tree](disclosure/drill_tree) — canonical tree navigation (walk's underlying op)
