---
name: knowledge-system
description: Unified agent self-documentation — file-per-unit Markdown store, typed units, DAG hierarchy, federated search
category: architecture
tags: [knowledge, documentation, progressive-disclosure, store, self-documentation, factorized]
tier1_summary: >
  All agent-facing content is one Markdown file per unit under knowledge/store/**/*.md.
  Typed units with a DAG hierarchy. Query via the agent_docs MCP capability; edit via the docs_edit workflow.
requires: []
---

# Knowledge System

All agent-facing content — behavioral directions, system docs, capability declarations,
workflow structure — lives in the knowledge store as **one Markdown file per unit** (YAML
frontmatter + Markdown body). Agents query this at runtime via `agent_docs` instead of
reading scattered files, and edit it via the `docs_edit` workflow.

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
| `capability` | MCP callable declaration — an Op (callable in `work_buddy/mcp_server/ops/`) plus a `kind: capability` unit naming it | task_create, session_search, context_bundle |
| `workflow` | Multi-step DAG — `steps` in frontmatter, per-step prose under `## <step-id>` body sections | task-triage, morning-routine, update-journal |

(Plus `service`, `integration`, `reference`, `concept`, and vault-backed `personal` units — nine kinds in all; see `architecture/knowledge-system` for the full taxonomy.)

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
| `<<wb:tasks/task-new --recursive>>` | Inserts task-new's content WITH its own placeholders resolved transitively |

Parsed with Python `argparse` — the first positional arg is the unit path, flags are CLI-style:
- `--recursive` — resolve the referenced unit's own placeholders transitively
- Future: `--depth=summary`, `--section=Definition`, etc.

**Missing refs** produce `<!-- wb: path not found -->`. Works in both system unit bodies and vault markdown files.

**Cycle detection** runs at store load time via networkx, covering parent/child edges and placeholder references. No runtime cycle tracking needed.

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

One Markdown file per unit at `knowledge/store/<path>.md` — the path↔file mapping is bijective (a unit at store path `P` lives at `knowledge/store/<P>.md`). A domain parent unit and its children's directory coexist (e.g. `tasks.md` beside `tasks/`); no `index.md` convention is needed. `children` is never stored — it is derived at load time from other units' `parents`.

```
knowledge/store/
  architecture/        # repo structure, workflows, knowledge system, ...
  tasks/               # task directions, triage, the task-* capability declarations
  context/             # collectors, docs-edit, docs_delete, agent_docs, ...
  morning/             # morning routine
  ...                  # one directory per domain; one .md per unit, every kind

knowledge/store.local/ # user patches (gitignored, JSON-shaped, deep-merged on load)
```

Every unit kind — directions, system, capability declarations, workflows — is a Markdown file authored the same way. The only JSON is `knowledge/store.local/*.json`: a gitignored personal-overlay layer deep-merged on top of the file-per-unit base at load.

## Adding or editing content

Use the **`docs_edit` workflow** — it returns the unit's `.md` path, you edit it with your native `Edit` tool, and the commit step validates (kind-aware) and reconciles the store cache + search index:

```
# Edit an existing unit
mcp__work-buddy__wb_run("docs-edit", {"path": "domain/my-directions"})

# Scaffold and author a new unit of any kind
mcp__work-buddy__wb_run("docs-edit", {"path": "domain/my-unit", "create": true, "kind": "directions"})
```

A unit file is YAML frontmatter (structured fields: `name`, `kind`, `description`, `parents`, `tags`, `aliases`, kind-specific fields, optional `dev_notes`) followed by the Markdown body (`content.full`). A **workflow** unit carries its `steps` DAG in frontmatter with per-step prose under `## <step-id>` sections. A **capability** unit is a declaration (`kind: capability` with `op`, `capability_name`, `category`, `parameters`) whose Op is registered in `work_buddy/mcp_server/ops/`.

A direct `Edit` of a unit's `.md` works too; follow it with `agent_docs_rebuild` so the store cache and search index pick up the change. Removing or relocating a unit (not a content edit) uses the `docs_delete` / `docs_move` capabilities.

## Search index

Knowledge search uses an in-memory index that fuses three independent ranking signals via Reciprocal Rank Fusion.

| Signal | Model | Purpose |
|--------|-------|---------|
| BM25 (dual) | — | Lexical match over full-text and metadata (weights 0.7 / 0.3) |
| Content dense | `leaf-ir` 768-d, asymmetric | Passage-shaped semantic match; queries encoded via `embed_for_ir(role="query")`, content via `role="document"` |
| Alias dense | `leaf-mt` 1024-d, symmetric | Query-shaped match against authored alias phrases; max-pooled per doc so one strong alias hit wins |

**Why three signals.** BM25 handles exact tokens. Content-dense catches queries that use different words than the docs. Alias-dense rescues queries that use different vocabulary than either — it's query-shaped on both sides (user query vs authored alias), which the symmetric model fits best.

**RRF fusion** is rank-based, so fusing across the 768-d and 1024-d spaces is safe (no score normalization needed). If any signal is unavailable (embedding service down, no aliases on a unit), it's dropped from fusion and the remaining signals still rank. BM25 alone is always a working fallback.

**Alias coverage matters.** Three-signal fusion is fair only when every capability has authored aliases. A cap with zero aliases gets two votes (BM25 + content) against aliased competitors' three — and loses. Keep the `aliases` field populated on every capability declaration unit; aim for 5-8 natural phrasings each (noun-phrase + question-shaped).

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
knowledge/store/                # File-per-unit Markdown store (one .md per unit)
work_buddy/knowledge/
  model.py                      # KnowledgeUnit type hierarchy (PromptUnit, VaultUnit)
  file_store.py                 # File-per-unit read/write seam + Markdown<->unit codec
  store.py                      # Store + vault loader, caching, invalidation
  index.py                      # In-memory BM25 + content-dense + alias-dense index (RRF)
  persistence.py                # Disk cache for content + alias vectors (hash-keyed)
  search.py                     # Federated search (4 modes), delegates to index
  query.py                      # MCP-facing read functions (knowledge, agent_docs)
  editor.py                     # Transactional CRUD layer (create/update/delete/move)
  edit_flow.py                  # docs_edit workflow step callables (resolve, commit)
  validate.py                   # Store integrity checks (docs_validate)
  __init__.py                   # Public API
```

The knowledge system integrates with the MCP registry:
- `search_registry()` delegates to the store for richer results
- Exact capability/workflow lookups fall through to the store when registry filtering removes them
- `knowledge` is the unified query interface; `agent_docs` for system docs only; legacy `docs_query`/`docs_get` still work
