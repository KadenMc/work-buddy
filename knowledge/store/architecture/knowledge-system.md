---
name: Knowledge System
kind: concept
description: Unified agent self-documentation — typed units, DAG hierarchy, full-content search index with BM25 + dense embeddings
summary: Two parallel stores (system Markdown + personal vault) sharing one KnowledgeUnit schema; DAG hierarchy; progressive disclosure; inline <<wb:path>> placeholders.
tags:
- knowledge
- documentation
- search-index
- BM25
- embeddings
- progressive-disclosure
aliases:
- knowledge store
- agent docs
- knowledge index
- knowledge search
- self-documentation
parents:
- architecture
- architecture
---

Two parallel knowledge stores share a common `KnowledgeUnit` base:

- **System docs** — one Markdown file per unit under `knowledge/store/**/*.md` (YAML frontmatter + body). Behavioral directions, system docs, capability declarations, workflow structure. Queried via `agent_docs`. Editing a unit is editing its file.
- **Personal knowledge** (Obsidian vault markdown at `<vault_root>/<personal_knowledge.vault_path>`) — user-authored patterns, feedback, preferences, calibration. Queried via `knowledge_personal`. Created/updated via `knowledge_mint`.

## Unit types

Nine kinds, each positively anchored to a clear functional or structural definition:

- `directions` — behavioral guide loaded by a slash command ("how to do X")
- `capability` — callable from MCP via `wb_run`; an inert declaration that names an `op` the loader resolves against the Op registry (see `architecture/data-first-capabilities`)
- `workflow` — DAG of steps the conductor advances; hand-authored
- `personal` — user-authored knowledge backed by the Obsidian vault
- `system` — coherent functional domain whose persistent state work-buddy owns (e.g. `tasks`, `triage`, `journal`)
- `service` — internal work-buddy component with a network surface (e.g. `services/dashboard`, `architecture/embedding-service`)
- `integration` — connection to an external system (e.g. `obsidian/bridge`, `email`, `vault`)
- `reference` — documents the API surface of one or more Python modules (entry-points-led)
- `concept` — architectural narrative, design philosophy, or domain-category heading

The `system` / `integration` boundary is anchored on **memory ownership**: a domain whose persistent state work-buddy itself manages is a `system`; a domain whose state lives outside (Obsidian vault, Thunderbird, etc.) is an `integration`, even when work-buddy provides operations against it.

The `service` / `integration` boundary is internal vs. external: a `service` is something work-buddy *runs* (sidecar-managed); an `integration` is something work-buddy *talks to* (the bridge that wraps it may be a Flask app, but the unit's identity is the external dependency).

## File substrate

A unit at store path `P` lives at `knowledge/store/<P>.md`. A domain parent unit and its children's directory coexist (e.g. `tasks.md` beside `tasks/`). The path↔file mapping is bijective; no `index.md` convention is needed.

`children` is **not stored** — it is derived at load time from other units' `parents`, so the parent/child graph cannot drift. Only `parents` is authored.

Workflow units carry their `steps` DAG in YAML frontmatter; each step's per-step instruction prose lives as a `## <step-id>` body section, split only at headings whose text exactly matches a known step id. Body text before the first step-id heading is the workflow's `content.full` narrative.

The engine talks to a narrow read/write seam (`read_unit`, `write_unit`, `list_unit_paths`, `delete_unit`, `move_unit`, `load_units_from_dir`) defined in `work_buddy/knowledge/file_store.py`. A future storage provider can implement the same seam without touching the engine.

Local patches live in `knowledge/store.local/*.json` (gitignored, JSON-shaped, deep-merged on top of the file-per-unit base on load) — the personal-overlay seam.

## DAG hierarchy and multi-parent nesting

Units have parents / children for hierarchical navigation. An agent querying `journal/` sees children without loading siblings it doesn't need.

**The `parents` field is a list, not a singleton.** The DAG validator allows multiple parents and only forbids cycles. A subsystem can live at one path for navigation and declare additional parent systems via `parents`. When creating a `system` unit that is a subsystem of a larger system, include the parent system in `parents` regardless of where the path lives. Path picks one navigation entry point; the DAG carries the full relationship graph.

The "subsystem-of-system" relationship is derivable: walk a unit's `parents` and filter by `kind == "system"`.

## Progressive disclosure

`depth="index"` (name + children list) → `"summary"` (core info) → `"full"` (complete content). Typical pattern: "index to map the domain, summary to triage, full for the one unit you're acting on."

## Inline placeholders

Content can reference other units inline. The syntax is two angle brackets, `wb:`, the target unit path, two angle brackets — e.g. a reference to `obsidian/bridge` is written as that path surrounded by the `wb:` prefix and angle-bracket markers. At `depth="full"`, the placeholder is replaced with the referenced unit's content. Appending ` --recursive` after the path opts in to transitive expansion. Parsed with argparse (extensible to `--depth`, `--section`, etc.).

### Caller-side knobs on `agent_docs`

Callers can override authorial defaults at query time:

- `recursive="default"` — each placeholder honours its own `--recursive` flag.
- `recursive="all"` — every placeholder expands transitively, ignoring per-flag choices.
- `recursive="none"` — placeholders are preserved literally (markup not resolved). Useful for editing or inspection.
- `max_depth=N` — caps recursion depth. `-1` (default) selects the mode default: unlimited in `default` mode, 10 in `all` mode. `0` disables recursion entirely. Positive ints set an exact cap.

The search corpus is always indexed with `recursive="default"` so search relevance doesn't shift with caller intent.

### Three safety mechanisms layered around recursive expansion

1. **Per-unit-occurrence cap** (always on, no override). First appearance of a target gets the full content; subsequent references — through any branch — return the inline marker `<!-- wb: <path> already expanded above -->`. Catches diamond graphs cheaply.
2. **Depth cap** (configurable via `max_depth`). On overflow, emits `<!-- wb: placeholder expansion truncated at depth N -->`.
3. **Size budget** (~100KB total expanded output in `all` mode). Ultimate backstop. On overflow, emits `<!-- wb: placeholder expansion truncated at 100KB cap -->`.

Each cap emits a distinct visible marker so the reader sees exactly what was elided and why.

### Authoring guardrails

- **Write-time hint (informational).** The internal `create_unit` / `update_unit` write primitives return a `hints` field flagging plain placeholders that target units with their own placeholders — the case where the author probably wanted `--recursive` but forgot. Never blocks an edit.
- **Write-time hard reject (error).** The same write path rejects content with **duplicate placeholders within a single unit**: the same target appearing more than once contributes zero readable content (the per-unit-occurrence cap renders subsequent references as back-ref markers), so it's never the right authorial choice. The editor returns `{"error": "placeholder_duplicate", "duplicates": [...]}` and does not persist.
- **Validator parity.** `docs_validate` runs a `placeholder_duplicate` check corpus-wide so direct-file bypasses are still caught.

## Retired: context chaining

A `context_before` / `context_after` mechanism on each unit (auto-prepend / -append of referenced units' content at `depth="full"`) was retired in favour of inline placeholders. Placeholders give authors per-reference control over recursion; chains were always hardcoded one-level. The fields are no longer read by the resolver; loaders silently ignore them if stale frontmatter still has the keys.

## Dev notes

Units can carry a `dev_notes` string — development-facing guidance that only surfaces when the agent is in dev mode (enabled via `mode_toggle`) or when the caller passes `dev=True`. Use for architectural constraints, non-obvious dependencies, and hard-won lessons that future agents could easily clobber. All subsequent knowledge queries in the session auto-include `dev_notes` once dev mode is active.

## Search index

A persistent BM25 + dense vector index over full unit content is warmed eagerly on MCP server startup. Inline placeholders are resolved before indexing so searching for content inside a referenced unit also surfaces the referencing unit. This powers `knowledge` and `agent_docs` search with hybrid ranking (keyword + semantic). See `architecture/embedding-service` for the model registry behind dense retrieval.

## MCP capabilities

- `knowledge` — unified search across both system docs and personal knowledge
- `knowledge_personal` — personal vault knowledge only (supports `category` and `severity` filters)
- `knowledge_mint` — create or update a personal knowledge unit in the vault
- `agent_docs_rebuild` — reload both stores from disk after edits
- `mode_toggle` — toggle a session mode (e.g. dev) on or off
- `knowledge_index_rebuild` — force rebuild knowledge search index with full embeddings
- `knowledge_index_status` — check index health
- `docs_edit` — the workflow for editing or creating **any** unit kind: it returns the unit's `.md` path, the agent edits it natively, and the commit step validates (kind-aware) and reconciles the store cache + index.
- `docs_delete` / `docs_move` — structural operations (remove / relocate a unit and reconcile parent references).
- `docs_validate` — kind-aware structural validation over the store (DAG, placeholder duplicates, capability op-resolution, workflow step-DAG).
