---
name: Projects
kind: system
description: Project registry — identity, observations, memory, discovery, and lifecycle
summary: Projects are registered entities tracked by slug. Each project has identity (name, status, description), observations (strategic notes retained into Hindsight), and memory (semantic recall via project_memory). Use project_list to browse, project_get for detail, project_observe to record decisions/pivots.
tags:
- projects
- registry
- hindsight
- observations
---

Projects are any bounded area of work or life the user tracks: research papers, coding repos, a book, a business, admin workflows. The project system captures understanding (what a project is, its current state, where it's heading) rather than substance (commit counts, task lists, dirty trees).

Four-layer architecture:
- Layer 1: Sync (signal scan) — vault dirs, STATE.md, task tags, git, contracts. Surfaces unregistered slugs as **candidates** in a separate bundle section; never auto-creates non-vault projects. See `work_buddy/projects/sync.py::sync_projects`.
- Layer 2: Identity Registry (SQLite, projects/projects.db) — a relational temporal store. Current state in three tables: `projects` (id, slug, name, status, description, origin, timestamps), `project_folders` (per-project absolute paths with `archived` flag), `project_aliases` (alternative slugs with display casing + normalized form for lookup).
- Layer 3: Revision History (append-only) — `project_revisions` captures the projects-row snapshot plus author (`user` or `agent`), `change_summary`, and `user_confirmed_at` for every mutation. `project_folders_history` and `project_aliases_history` capture the child-table state at each revision. Every mutation writes one revision row + snapshots of the variable-length parts, all in one transaction.
- Layer 4: Project Memory (Hindsight bank: project-memory) — LLM-extracted facts from observations, embedding-based semantic recall, mental models. Recall is alias-aware: querying a project's slug unions memory tags for the canonical slug plus all its aliases, so historically-tagged memories still surface after a rename. Applies to semantic recall (`recall_project_context`, `recall_project_context_items`) and to the chronological listing (`list_recent_project_memories`) used by the dashboard's project-detail memory log. Semantic recall pays for embeddings and should be reserved for relevance-ranked retrieval into an LLM prompt; chronological listing is free of embedding cost and the right primitive for any UI that just shows "what's been recorded here."

Key principle: Cheap identity, deep memory on demand. Always inject the project list (free SQLite query), let agents explore memory when they need it.

Lifecycle: `active` → `paused` → `past` (plus `future` for planned-not-yet-started; plus `deleted` for soft-delete). The canonical display order is `STATUS_DISPLAY_ORDER` in `store.py` — the SQL ORDER BY, bundle section order, and dashboard schema endpoint all derive from this single constant. Status is store-owned; sync never overwrites it. Soft-deleted rows preserve their revision history and remain queryable via `include_deleted=True`; default queries filter them out.

Provenance: each row carries `origin` (`vault` if scan discovered a canonical directory, `manual` if registered explicitly) and each revision carries `author` (`user` or `agent`). The `user_confirmed_at` flag on a revision records when a human approved an LLM-authored change — the data model for review-and-confirm UX.

Slugs are renameable: the surrogate integer `id` is the stable identifier, slug is a mutable UNIQUE label. Aliases capture prior names (e.g. `ElectricRAG`, `ECG-CRED`) so `resolve_slug` routes them to the canonical row. Aliases prefer non-deleted canonical matches over deleted ones.

**Git attribution is folder-driven**: each registered project's git activity comes from scanning the `.git/` directory inside its non-archived folders, not from matching the repo's folder name against the slug. This handles cases like `ecg-fm` ↔ `repos/foundational-ecg/` where the repo name differs from the project slug, and folders outside `repos_root` entirely. The legacy slug-matched scan still runs as a candidate-discovery surface for unregistered repos. See `_scan_git_activity` (candidate discovery) + `_read_git_repo_activity` (per-folder probe) in `sync.py`.

**Activity scoring** (`work_buddy/projects/activity.py`): the dashboard's `/api/projects` endpoint sorts active rows by an exponentially-decayed score combining project_revisions (weight 1.5), folder mtimes (weight 2.0), and git commits (weight 1.0). Half-life 14 days, window 60 days. Score is computed on demand with a 5-minute per-folder git cache; non-active rows are not scored.

Tag taxonomy (Hindsight): project:{slug}, source:chat|collector|state-file|user, session:{id}. Slugs in tags are subject to rename drift — alias union at query time handles it; a UUID-based tag scheme is a planned future migration.

Integration points — project list is injected into: context collection (collect-and-orient), morning routine (context-snapshot), chrome triage (build_triage_context), task triage (gather step), task briefing (daily_briefing), blindspot detection (gather-signals), journal backlog routing, weekly review (gather step).

Dashboard surfaces (`work_buddy/dashboard/service.py`): `/api/projects/_schema` exposes enum metadata derived from store constants. `/api/projects` returns activity-sorted projects with folder existence flags + structured memory items. Mutation endpoints under `/api/projects/<slug>/{folders,aliases}` route through the store API so every dashboard edit writes an authored revision; the frontend supports inline edit, add, remove, and archive-toggle.

File layout: `work_buddy/projects/` (store.py for SQLite CRUD + revision-writing, migrations.py for the versioned schema ladder, sync.py for the multi-signal sync job + folder-driven git, activity.py for the score), `work_buddy/memory/` (ingest.py, query.py for Hindsight — alias-aware semantic recall + chronological listing), `work_buddy/ir/sources/projects.py` (IR source adapter). The legacy `work_buddy/collectors/project_collector.py` is a back-compat shim re-exporting from `projects/sync.py`.
