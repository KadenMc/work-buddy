---
name: MarkdownDB
kind: concept
description: Markdown-canonical two-way markdown <-> SQLite sync abstraction. Subclass per entity (FieldSpec list + parse/render); the base supplies orphan handling, the per-field drift loop, LWW conflict resolution, dual-surface mutation, and materialization. Backed by an append-only lww_meta write-provenance sidecar.
tags:
- markdown-db
- sync
- reconciliation
- markdown-canonical
- lww
- lww-meta
- write-provenance
- sqlite
- drift
- two-way-sync
- abstraction
aliases:
- MarkdownDB
- markdown db
- markdown-db
- two-way sync
- markdown sync
- lww_meta
- WriteProvenance
- FieldSpec
- TaskMarkdownDB
- ProjectMarkdownDB
- reconcile_drift
- SqliteLwwLog
- drift reconciliation
parents:
- architecture
- architecture
dev_notes: 'Extracted from obsidian/tasks/sync.py. Both subclasses are cut over. ProjectMarkdownDB: the dashboard project-edit path routes through apply_mutation; the project-sync cron reconciles out-of-band Obsidian edits. TaskMarkdownDB: sync.task_sync() delegates to reconcile_tasks(), so the markdown_db abstraction is the production task reconciler; reconcile_drift''s post_reconcile hook does the tag-cache rebuild + task_sync_status write. The 8 loops in the old task_sync body were deleted; sync.py keeps only its parsing/tag helpers, which TaskMarkdownDB reuses (referenced via the sync module, not by-value, so monkeypatching reaches them). ProjectMarkdownDB.delete_orphans_in_store stays False (deleting a project note does not delete the project); flipping it is a separate decision. Both subclasses default to NullLwwLog — wiring SqliteLwwLog in is optional. Open design question: ProjectMarkdownDB lww_meta vs the projects store''s own project_revisions history — decide which owns cross-surface conflict resolution. Mass-delete circuit-breaker (reconcile_drift): keys on delete MAGNITUDE not ''parse==0'' — empty parse is just the limiting case; a partial-read/regression leaves a small-but-nonzero parse a zero-check would miss. Floor of 20 lets small stores delete normally (1-of-1 is not a ''mass'' delete). Follow-up: a trip is visible via the capability status + ERROR log but NOT the task_sync_status freshness row (fixed columns) — stamping ''degraded'' there needs a schema column.'
---

`MarkdownDB` is work-buddy's markdown-canonical two-way synchronisation abstraction. It lives in `work_buddy/markdown_db/` and was extracted from the bespoke task reconciler in `obsidian/tasks/sync.py`.

## The model

Markdown is the **canonical** store; a SQLite table is a queryable projection of it. Two write paths: in-code writes (agent, dashboard) go through `MarkdownDB.apply_mutation`, which writes BOTH surfaces atomically (markdown first — markdown-ahead is the safe failure direction) and stamps the LWW log; out-of-band writes (a human editing in Obsidian) are caught later by `MarkdownDB.reconcile_drift`, the periodic safety net.

## Subclassing

A subclass declares a list of `FieldSpec`s (one per reconcilable field), `table_name` / `pk_column`, and implements `parse_all_from_markdown`, `write_entity_to_markdown`, `markdown_path_for`. The base class supplies orphan handling, the generic per-field drift loop, conflict resolution, dual-surface mutation, and `materialize_from_store` (the one-time store->markdown flip). `FieldSpec` carries optional hooks for shapes that are not a clean value mirror: `equivalent` (custom in-sync predicate — the task checkbox is a lossy projection of the 5-valued `state`) and `extra_store_fields` (derived columns written in lockstep — `deadline_date` keeps `has_deadline` consistent).

## Conflict resolution

Pluggable `Resolver`. Default `lww_markdown_wins`: newer timestamp wins; on a tie or missing timestamps the markdown surface wins. Isolating resolution behind one callable is the cheap CRDT-smoothing move — a richer resolver is a one-symbol swap.

## The lww_meta sidecar

`WriteProvenance` (actor as an OR-set `frozenset` honestly encoding partial observability; open-vocabulary `process` / `from_surface`) is stamped per write event into an append-only `lww_meta` table. `SqliteLwwLog` persists it INSIDE each entity's own DB so it travels with the `architecture/backups` tarball. The table is genuinely append-only (autoincrement id PK) — it serves LWW today and is replayable as an op log if a CRDT resolver is ever introduced. `NullLwwLog` (the default) makes a `MarkdownDB` behave as pure markdown-canonical with no LWW history.

## Concrete subclasses

`TaskMarkdownDB` (`obsidian/tasks/markdown_db.py`) — the task master list <-> `task_metadata`; six `FieldSpec`s replace the eight hand-written loops in the legacy `task_sync`. `ProjectMarkdownDB` (`projects/markdown_db.py`) — one `<slug>.md` note per project in a single flat vault directory (`projects.markdown_dir` in config, default `work-buddy/projects`, a sibling of `contracts.vault_path`) <-> the projects store; `materialize_projects()` performs the first-run store->markdown flip (dry-run by default). The notes directory is a Repository-Setup health requirement (`core/config/projects-markdown-dir`).

## Mass-delete circuit-breaker

"Markdown is canonical" means a store row absent from the parse is soft-deleted — correct only if the parse is trustworthy. `reconcile_drift` refuses any single-pass orphan-delete batch larger than `max(20, 50% of the live store)`, so a degraded read (a bridge blink, a partial/truncated read, a parser/ID-scheme regression) can never silently wipe the store. A refusal skips ONLY the delete loop (creates + field-drift still run), sets `ReconcileReport.aborted_bulk_delete = (would_delete, live_count)`, logs at ERROR, and surfaces as a `task_sync` status of `"degraded"` (not the healthy `"ok"`/`"synced"`). The next reconcile retries with hopefully-sane input.

## Status

Both subclasses are CUT OVER. `TaskMarkdownDB`: `obsidian.tasks.sync.task_sync()` delegates to it (via `reconcile_tasks`), so it is the reconciler the `task-sync` cron and the dashboard Sync button run; task mutations (`update_task` etc.) already wrote both surfaces. `ProjectMarkdownDB`: the dashboard project-edit path (`POST /api/projects/<slug>`) routes through `apply_mutation` so an edit writes both surfaces, and the `project-sync` cron (every 30 min, jittered) reconciles out-of-band Obsidian note edits. `lww_meta` ships via `task_metadata` migration v10 and `projects` migration v7 — DDL inlined in each callable so the runner's hash audit covers it, kept byte-identical to `markdown_db.sqlite_lww.LWW_META_DDL`.
