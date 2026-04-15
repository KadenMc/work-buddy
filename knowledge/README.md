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

Knowledge search uses a dedicated in-memory index that searches the **full content**
of every unit (metadata + summary + body), not just metadata phrases.

| Component | What it does |
|-----------|-------------|
| BM25 (dual) | Two parallel BM25Okapi indices: content-weighted (0.7) + metadata-weighted (0.3) |
| Dense vectors | Full-content embeddings via the embedding service (1024-dim, ~0.76 MB for 220 units) |
| RRF fusion | Reciprocal Rank Fusion when both BM25 and dense are available |

**Lifecycle:**
- Built eagerly on MCP server startup (BM25 inline ~50ms, dense in background thread ~3.5s)
- Invalidated automatically when `invalidate_store()` or `invalidate_vault()` is called
- Rebuilt on next search query or explicit `knowledge_index_rebuild`
- Generation guards prevent stale background threads from corrupting a rebuilt index

**MCP capabilities:**
- `knowledge_index_rebuild` — force rebuild with full embeddings
- `knowledge_index_status` — check index health (built, unit count, dense available)

**Fallback:** If embedding service is down at build time, index uses BM25 only.
If down at query time, BM25 still works. Dense vectors are never required.

## Architecture

```
knowledge/store/                # Canonical JSON store
work_buddy/knowledge/
  model.py                      # KnowledgeUnit type hierarchy (PromptUnit, VaultUnit)
  store.py                      # JSON + vault loader, caching, invalidation
  index.py                      # In-memory BM25 + dense search index
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
