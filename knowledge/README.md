---
name: knowledge-system
description: Unified agent self-documentation — JSON store, typed units, DAG hierarchy, federated search
category: architecture
tags: [knowledge, documentation, progressive-disclosure, store, self-documentation, factorized]
tier1_summary: >
  All agent-facing content in knowledge/store/*.json. Four unit types with DAG hierarchy.
  Query via agent_docs MCP capability. Build generates capability/workflow units from registry.
requires: []
---

# Knowledge System

All agent-facing content — behavioral directions, system docs, capability metadata,
workflow structure — lives in a canonical JSON store. Agents query this at runtime via
`agent_docs` instead of reading scattered files.

## Quick start

```
# Browse the full index
mcp__work-buddy__wb_run("agent_docs", {})

# Search by topic
mcp__work-buddy__wb_run("agent_docs", {"query": "triage my tasks"})

# Navigate a subtree
mcp__work-buddy__wb_run("agent_docs", {"scope": "tasks/"})

# Direct lookup
mcp__work-buddy__wb_run("agent_docs", {"path": "morning/directions", "depth": "full"})

# Rebuild after edits
mcp__work-buddy__wb_run("agent_docs_rebuild", {})
```

## Unit types

| Kind | What it is | Examples |
|------|-----------|----------|
| `directions` | Behavioral "how to do X" — migrated from slash commands | triage rules, journal synthesis format, morning sign-in tone |
| `system` | Reference "what is X" — architecture, integration guides | Obsidian bridge, consent system, repo structure |
| `capability` | MCP callable metadata — auto-generated from registry | task_create, session_search, context_bundle |
| `workflow` | Multi-step DAG — auto-generated from workflow files | task-triage, morning-routine, update-journal |

## DAG hierarchy

Units form a directed acyclic graph (multiple parents, multiple children, no cycles).
Navigation works by browsing children:

```
tasks/                          <- browse this
  tasks/triage-directions       <- agent selectively loads this
  tasks/task-triage             <- ...or this (the workflow)
  tasks/task_create             <- ...but never touches this if irrelevant
  tasks/task_briefing
  ...
```

## Progressive disclosure

| Depth | Returns | Tokens |
|-------|---------|--------|
| `"index"` | name + description + kind + children list | ~50 |
| `"summary"` | above + content["summary"] + kind-specific fields | ~200-500 |
| `"full"` | above + content["full"] + all fields | unbounded |

Typical pattern: search at index depth → browse children → load summary for 2-3 → load full for the one being acted on.

## Inline placeholders

Content strings can reference other units inline using `<<wb:path>>` syntax.
At `depth="full"`, placeholders are resolved to the referenced unit's content.

**Syntax:** `<<wb:unit-path --flags>>`

| Placeholder | Behavior |
|-------------|----------|
| `<<wb:obsidian/bridge>>` | Inserts the bridge unit's raw content at this position |
| `<<wb:tasks/task-new --recursive>>` | Inserts task-new's content WITH its own placeholders and context chains resolved |

Parsed with Python `argparse` — the first positional arg is the unit path, flags are CLI-style:
- `--recursive` — resolve the referenced unit's own chains transitively
- Future: `--depth=summary`, `--section=Definition`, etc.

**Missing refs** produce `<!-- wb: path not found -->`. Works in both JSON content strings and vault markdown files.

**Context chaining** (`context_before` / `context_after`) still works — use inline placeholders when you need precise mid-document placement or recursive resolution.

**Cycle detection** runs at store load time via networkx, covering parent/child edges, context chains, AND placeholder references. No runtime cycle tracking needed.

**Search index** resolves placeholders before indexing, so searching for content in a referenced unit also surfaces the referencing unit.

### Example: handoff → task-new → bridge

```
handoff-directions content:
  "Create the task... <<wb:tasks/task-new-directions --recursive>>"

task-new-directions content:
  "Create via task_create... <<wb:obsidian/bridge>>"

Resolved at depth="full":
  handoff gets task-new's content (which itself includes bridge content)
  — single source of truth, no duplication
```

## Store layout

```
knowledge/store/
  operations.json     # MCP gateway, agent sessions
  obsidian.json       # bridge, vault writer
  notifications.json  # consent, surfaces, telegram
  services.json       # dashboard, messaging, memory
  architecture.json   # repo structure, workflows, contracts
  tasks.json          # triage directions
  journal.json        # update directions
  context.json        # collect directions
  morning.json        # morning routine directions

  _generated_capabilities.json   # auto-generated from registry
  _generated_workflows.json      # auto-generated from workflow files
  _generated_parents.json        # auto-generated container nodes

knowledge/store.local/           # user patches (gitignored)
```

Hand-authored files contain `directions` and `system` units.
Generated files (prefixed `_generated_`) contain `capability` and `workflow` units.

## Adding content

### New directions unit
Add to the appropriate domain JSON file in `knowledge/store/`:
```json
{
  "domain/my-directions": {
    "kind": "directions",
    "name": "My Directions",
    "description": "One-line summary for search",
    "aliases": ["alternative phrasings", "for search"],
    "tags": ["domain", "keywords"],
    "parents": ["domain"],
    "trigger": "when to use these directions",
    "command": "wb-my-command",
    "workflow": "domain/my-workflow",
    "capabilities": ["domain/relevant_cap"],
    "content": {
      "summary": "Brief overview...",
      "full": "Complete behavioral directions..."
    }
  }
}
```

### New system unit
Same pattern but with `"kind": "system"` and optional `ports`, `entry_points` fields.

### Regenerate capability/workflow units
```bash
python -m work_buddy.knowledge.build --write
```

## Search index

Knowledge search uses an in-memory index that fuses three independent ranking signals via Reciprocal Rank Fusion.

| Signal | Model | Purpose |
|--------|-------|---------|
| BM25 (dual) | — | Lexical match over full-text and metadata (weights 0.7 / 0.3) |
| Content dense | `leaf-ir` 768-d, asymmetric | Passage-shaped semantic match; queries encoded via `embed_for_ir(role="query")`, content via `role="document"` |
| Alias dense | `leaf-mt` 1024-d, symmetric | Query-shaped match against authored alias phrases; max-pooled per doc so one strong alias hit wins |

**Why three signals.** BM25 handles exact tokens. Content-dense catches queries that use different words than the docs. Alias-dense rescues queries that use different vocabulary than either — it's query-shaped on both sides (user query vs authored alias), which the symmetric model fits best.

**RRF fusion** is rank-based, so fusing across the 768-d and 1024-d spaces is safe (no score normalization needed). If any signal is unavailable (embedding service down, no aliases on a unit), it's dropped from fusion and the remaining signals still rank. BM25 alone is always a working fallback.

**Alias coverage matters.** Three-signal fusion is fair only when every capability has authored aliases. A cap with zero aliases gets two votes (BM25 + content) against aliased competitors' three — and loses. Keep `search_aliases` populated on every `Capability` in `registry.py`; aim for 5-8 natural phrasings each (noun-phrase + question-shaped).

### Persistence

Vectors are cached to disk at `data/cache/knowledge_index/`:
- `content.npz` — one 768-d vector per unit, keyed by `path → (content_hash, vector)`
- `aliases.npz` — one 1024-d vector per alias, keyed by `(path, alias_text) → vector`
- Each file has a `model_key` + `CACHE_VERSION` header; mismatch → treated as empty (safe re-embed)
- Atomic writes via temp-file rename

On rebuild, only changed or new units re-embed. Typical warm restart is <1s. Cold first build is ~90-180s (the 526 MB `leaf-ir` document encoder lazy-loads once).

### Lifecycle

- Built eagerly on MCP server startup: BM25 inline (~100 ms), dense vectors in a background daemon thread (cache-hit rebuild ~0.4 s; cold first rebuild ~90-180 s)
- Invalidated automatically when `invalidate_store()` or `invalidate_vault()` is called
- Rebuilt on next search query or explicit `knowledge_index_rebuild(force=false)` (uses cache); pass `force=true` to purge the cache and re-embed from scratch
- Generation guards at both start and commit prevent stale background threads from overwriting a rebuilt index

### MCP capabilities

- `knowledge_index_rebuild(force: bool = False)` — rebuild using cache (fast) or from scratch (`force=true`, slow)
- `knowledge_index_status` — index health + cache file sizes

## Architecture

```
knowledge/store/                # Canonical JSON store
work_buddy/knowledge/
  model.py                      # KnowledgeUnit type hierarchy (PromptUnit, VaultUnit)
  store.py                      # JSON + vault loader, caching, invalidation
  index.py                      # In-memory BM25 + content-dense + alias-dense index (RRF)
  persistence.py                # Disk cache for content + alias vectors (hash-keyed)
  search.py                     # Federated search (4 modes), delegates to index
  query.py                      # MCP-facing functions (knowledge, agent_docs)
  build.py                      # Registry → store generator
  vault_adapter.py              # Markdown → VaultUnit loader
  __init__.py                   # Public API
```

The knowledge system integrates with the MCP registry:
- `search_registry()` delegates to the store for richer results
- Exact capability/workflow lookups fall through to the store when registry filtering removes them
- `knowledge` is the unified query interface; `agent_docs` for system docs only; legacy `docs_query`/`docs_get` still work
