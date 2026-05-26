# Phase 3 — Disclosure / Search Cohesion

A follow-up refactor to collapse the search/navigate/read surface into a
small set of universal primitives. Scoped after Phase 2 (PR #131) so the
foundation is in place; deliberately deferred there to avoid mixing
mergeable framework work with breaking consumer-API changes.

## Context

- **Author session:** Claude Code session `73aea139-c4bc-4210-8ce4-debacfba976f`
  (2026-05-25 overnight build).
- **Builds on:**
  - PR #130 (Phase 1) — composition-based summarization framework:
    `Summarizer = Source × Strategy × Store`. Tree-shaped `SummaryNode`
    records persisted to `summarization.db`.
  - PR #131 (Phase 2) — progressive-disclosure layer over those summaries:
    IR `summary` source, `summary_search` funnel, unified `drill_tree`
    contract, `session-identify` directions reworked onto the funnel.
- **Source design discussion:** the AFK conversation under the
  `73aea139…` session captures the full design walkthrough — search for
  "Phase 3" / "cohesion" topics via
  `summary_search(query="cohesion analysis", scope="conversation_session")`.
  See also `.data/designs/progressive-disclosure/AFK-DECISIONS.md` (D1–D14)
  for the Phase-2 decisions that constrain Phase 3.

## The problem Phase 3 solves

The Phase 2 work shipped a working layered system but accumulated more
surface than is needed. Eight+ capabilities sit on the search /
navigate / read axis with real overlap:

| Capability | Role today | Phase-3 destination |
|---|---|---|
| `context_search` | universal IR search across any indexed source | **kept** — becomes `find` (or stays named `context_search` with an added drill flag) |
| `summary_search` | summary-domain IR search + per-namespace drill orchestration | **absorbed** into `context_search` via a per-source drill-handler registry |
| `drill_tree` | universal tree navigation across registered `TreeDrillable`s | **kept** — already the right primitive; optionally rename to `walk` |
| `agent_docs(path=...)` | knowledge-unit lookup by path | **absorbed** into `drill_tree(domain="knowledge")` |
| `agent_docs(query=...)` | knowledge-unit search | **absorbed** into `find(source="knowledge")` |
| `session_search` | hybrid IR search within one session | **convenience-keep** — thin wrapper over `find(source="conversation", scope=session_id)` + span-to-turn resolution |
| `session_get`, `session_expand`, `session_locate` | session-specific linear-read primitives | **kept** — sessions are sequence-shaped, not trees |
| `conversation_observability_summary_get` | legacy single-session row read | **deprecate** after consumers migrate; rename to `session_summary_get` if kept |

Pluggable details / second-order issues:

- `context_drill_down` (field-keyed) and `wb_step_result` (elided
  workflow result) are NOT in this phase. They are not tree-shaped and
  not search-shaped; they live alongside as their own niches. May wrap
  later if a consumer wants them in the unified surface.
- The vocabulary inconsistency between IR `doc_id` (`{ns}:{id}:n{ord}`)
  and disclosure `node_id` (`{ns}:{id}#n{ord}`) was already mitigated in
  Phase 2 by surfacing a pre-built `drill_node_id` field on every
  `summary_search` hit. Phase 3 preserves this; no consumer should ever
  see the format difference.

## End-state vision

Two universal verbs covering everything tree- or search-shaped:

```
find(query, source?, scope?, drill=False, top_k=10, method="keyword,semantic")
    → ranked hits from any indexed source.
    → drill=True triggers a per-source drill handler (registered via a
      simple registry mirroring TreeDrillable). For unregistered sources,
      drill is a silent no-op.

walk(domain, node_id, depth="index")
    → tree navigation across any registered TreeDrillable.
    → Three depths: index | summary | full.
```

Plus three sequence-specific session reads (kept separate because
sessions aren't trees):

```
session_get(session_id, offset, limit)              # linear browse
session_expand(session_id, message_index, span=5)   # neighborhood
session_locate(session_id, span_index)              # span → turn translation
```

Properties:

- **Source-optional**: a domain that doesn't register a `TreeDrillable`
  still works via `find`. A domain that doesn't register an IR source
  still works via `walk`. Neither is required; each unlocks an axis.
- **No duplicate verbs**: `find` is the only search; `walk` is the only
  tree navigation.
- **Composable hand-offs**: a `find` hit carries `walk_node_id` (the
  pre-built coordinate, already present as `drill_node_id` on
  `summary_search` hits — generalized to apply to any tree-shaped source).
  A `walk` response includes child node_ids you can pass back into
  `find` to search within.
- **Topic-agnostic naming**: no `summary_` prefix on universal verbs.

## Migration plan

Three sub-phases, each independently mergeable.

### Phase 3a — Generalize the drill handler (non-breaking)

Goal: make the funnel's drill stage source-agnostic, so any source can
register a drill handler.

1. Extract `summary_search`'s `_default_drill_handler` into a
   per-source registry similar to `disclosure/registry.py`:
   `register_drill_handler(source, handler_fn)`. Existing
   `conversation_session` handler routes to `session_search` as today.
2. Add an optional `drill=True` flag to `context_search`. When set, the
   IR engine runs the registered drill handler per top hit (if one is
   registered for `source`). For sources without a registered handler,
   `drill=True` is a silent no-op (logged at DEBUG).
3. `summary_search` becomes a thin alias: `context_search(source="summary", scope=..., drill=...)`.
   The capability declaration stays for back-compat for now.
4. Knowledge unit updates: `context_search` documents the optional
   drill behavior; `summary_search` becomes a redirect/alias unit.

**Test coverage:** new unit tests for the drill-handler registry + the
generalized `context_search` with `drill=True`. The existing
`summary_search` tests still pass against the alias.

**No breaking changes:** every consumer keeps working.

### Phase 3b — Migrate consumers off legacy compat surfaces (breaking-with-care)

Goal: deprecate `conversation_observability_summary_get` by migrating
its three known consumers.

Consumers and their expected row shape:

| Consumer | File | What it needs |
|---|---|---|
| Dashboard topics endpoint | `work_buddy/dashboard/service.py:1140` (`/api/chats/<id>/topics`) | flat dict: `tldr`, `topics: [...]` with `turn_range` |
| `claude_session_summary` context collector | `work_buddy/collectors/claude_session_summary_collector.py:145` | same flat dict |
| The op itself | `mcp_server/ops/conversation_observability_ops.py:101` | callable via `wb_run("conversation_observability_summary_get", ...)` |

Migration approach:

1. Add a small adapter `legacy_row_from_tree_view(view) -> dict` next to
   `summaries.py` that builds the legacy row from a `drill_tree(domain="summary",
   node_id=..., depth="summary")` response. Includes the lazy
   `turn_range` conversion via `span_range_to_turn_range`.
2. Migrate the dashboard endpoint to call
   `drill_tree(domain="summary", node_id=f"conversation_session:{sid}", depth="summary")`
   internally, then the adapter. Verify the JSON shape is byte-identical
   to the current response.
3. Migrate the context collector similarly.
4. Decision point: do we keep `conversation_observability_summary_get`
   as a public op (renamed to `session_summary_get`) or remove it
   entirely? Recommendation: rename to `session_summary_get`, keep as a
   thin wrapper that calls `drill_tree` + adapter for any external
   callers. Removes the long namespace prefix; preserves the convenience.

**Breaking changes:** internal dashboard/collector implementation. No
external API change if we rename + keep the wrapper.

### Phase 3c — Unify search/walk vocabulary (breaking-with-aliases)

Goal: introduce `find` and `walk` as the canonical names.

1. Register `find` as a new op that aliases `context_search`. Same
   parameter shape, same return shape. New capability declaration at
   `search/find` (or similar canonical location).
2. Register `walk` as a new op that aliases `drill_tree`. Same shape.
3. Mark the old names as "deprecated alias" in their knowledge units
   (still works, recommended replacement noted).
4. Migrate the slash-command directions (`session-identify`,
   `dev-orient`, etc.) to use the new names.
5. After ~a release cycle of operational stability, retire the old
   capability declarations.

**Breaking changes**: deprecation warnings on the old names; no
functional break.

### Phase 3d — `agent_docs` consolidation

Goal: fold `agent_docs(path=...)` into `walk(domain="knowledge", ...)`
and `agent_docs(query=...)` into `find(source="knowledge")`.

1. Add a `KnowledgeIRSource` so the knowledge store becomes a registered
   IR source (it already has a parallel BM25+dense index via
   `work_buddy/knowledge/`; this just bridges to the IR engine's source
   protocol).
2. `find(source="knowledge", query=...)` returns ranked knowledge units
   in the same shape as other `find` results.
3. `agent_docs(query=..., path=...)` becomes a wrapper that dispatches
   to `find` or `walk` internally. Its public capability surface stays
   for back-compat.
4. Knowledge store search index might already be sufficient; this is a
   bridging task more than a re-implementation.

Phase 3d is the lowest priority — `agent_docs` works well today, and
its current behavior is the heaviest-used path in the system. Touch
only if 3a-3c land cleanly and there's appetite.

## Open questions for the design pass when Phase 3 actually starts

1. **Should `walk` support a query argument?** Walking with a query
   becomes "navigate at depth, then within that subtree run search." It
   would obviate `find` + scope for many cases. Risk: feature bloat;
   muddies the verb's identity. Recommendation: defer unless an
   evidence trail of repeated `find` + `walk` chains argues for it.
2. **Renaming policy.** Soft (alias for one release) vs. hard
   (deprecate and remove). Work-buddy convention to be confirmed; tend
   toward soft so external consumers (slash commands, the dashboard)
   have time to migrate.
3. **`session_*` consolidation.** Today three capabilities cover
   linear browse, neighborhood, and span-to-turn translation. Could
   they collapse into one `session_read` with mode flags? Probably yes,
   but it's marginal value vs. churn. Defer.
4. **Drill handler return shape.** Today's handler returns whatever
   `session_search` returns. Generalizing to any source means defining
   a stable per-hit drill-result shape. Suggested: `{hits: [{score,
   payload, ...}]}`. Each source's handler conforms.
5. **Per-source drill registration timing.** Eager (at module import,
   like artifact registrations) vs. lazy (first use). Eager is simpler;
   lazy avoids importing every domain. Eager wins by default; revisit
   if the import set grows.

## Implementation order

Strict dependency chain:

```
3a (drill registry + context_search drill flag)
  ↓
3b (migrate consumers, deprecate summary_get)
  ↓
3c (find/walk aliases)
  ↓
3d (agent_docs consolidation, OPTIONAL)
```

Approximate effort, eyeball: 3a — 1 day; 3b — 1 day (mostly testing the
adapter against the dashboard); 3c — half a day (mechanical aliasing);
3d — 1-2 days (the IR-source bridge is the biggest piece).

## Out of scope for Phase 3

- Refactoring `context_drill_down` (field-keyed; not tree-shaped). Stays
  as a niche capability.
- Wrapping `wb_step_result` (sequence-of-steps; no consumer asking).
  Stays as a niche capability.
- Dashboard frontend changes beyond the JSON-shape preservation in
  Phase 3b. A "find session by topic" UI surface is a separate UX
  design pass.
- Removing the legacy `conversation_observability/session_summaries`
  and `topic_summaries` SQLite tables. Tracked separately as the
  shakeout-period follow-up from PR #130.
- Fixing the inert `feature_gated` sidecar mechanism. Tracked
  separately as task `t-b7d1b6ac`.

## Acceptance criteria

Phase 3 is complete when:

- An agent reaching for search reaches `find` (or its alias) regardless
  of source. Search across `conversation`, `summary`, `knowledge`,
  `chrome`, `task_note`, `docs`, `projects` looks the same.
- An agent reaching for tree navigation reaches `walk` (or its alias)
  regardless of domain. Knowledge units and framework summaries
  navigate identically.
- A new tree-shaped or search-indexed source plugs in via
  `register_drillable` + `IR source` registration with NO new
  capability declaration — agents discover it via the unified verbs.
- `conversation_observability_summary_get` is removed OR renamed to
  `session_summary_get` and reduced to a thin compat wrapper over
  `walk` + adapter.
- Knowledge-unit cross-references between `find` / `walk` / session-read
  primitives make the decision rule explicit at the discovery surface.
- 100% of Phase-2 tests pass unchanged; new tests cover the unified
  verbs.
