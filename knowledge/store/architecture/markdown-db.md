---
name: MarkdownDB
kind: concept
description: Frozen legacy Markdown-to-SQLite reconciliation abstraction retained for pre-seal compatibility and migration inspection.
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
dev_notes: 'Do not use this abstraction for new personal-content domains. Native Tasks and sealed Projects resolve authority before every operation and fence these reconcilers. Keep the implementation only for deterministic legacy import, pre-seal compatibility, and historical tests until the archive-retention window closes.'
---

`MarkdownDB` is Work Buddy's former Markdown-canonical two-way synchronisation abstraction. It lives in `work_buddy/markdown_db/` and was extracted from the bespoke task reconciler in `obsidian/tasks/sync.py`. It is no longer an approved authority pattern.

## The model

In the legacy model, Markdown was canonical and SQLite was a queryable projection. In-code writes touched both surfaces and periodic reconciliation consumed out-of-band file edits. Authority seals now prevent those paths from running for migrated domains. Native SQLite revisions and explicit Co-work bindings provide one writable body per role.

## Subclassing

A subclass declares a list of `FieldSpec`s (one per reconcilable field), `table_name` / `pk_column`, and implements `parse_all_from_markdown`, `write_entity_to_markdown`, `markdown_path_for`. The base class supplies orphan handling, the generic per-field drift loop, conflict resolution, dual-surface mutation, and `materialize_from_store` (the one-time store->markdown flip). `FieldSpec` carries optional hooks for shapes that are not a clean value mirror: `equivalent` (custom in-sync predicate — the task checkbox is a lossy projection of the 5-valued `state`) and `extra_store_fields` (derived columns written in lockstep — `deadline_date` keeps `has_deadline` consistent).

## Conflict resolution

Pluggable `Resolver`. Default `lww_markdown_wins`: newer timestamp wins; on a tie or missing timestamps the markdown surface wins. Isolating resolution behind one callable is the cheap CRDT-smoothing move — a richer resolver is a one-symbol swap.

## The lww_meta sidecar

`WriteProvenance` (actor as an OR-set `frozenset` honestly encoding partial observability; open-vocabulary `process` / `from_surface`) is stamped per write event into an append-only `lww_meta` table. `SqliteLwwLog` persists it INSIDE each entity's own DB so it travels with the `architecture/backups` tarball. The table is genuinely append-only (autoincrement id PK) — it serves LWW today and is replayable as an op log if a CRDT resolver is ever introduced. `NullLwwLog` (the default) makes a `MarkdownDB` behave as pure markdown-canonical with no LWW history.

## Concrete subclasses

`TaskMarkdownDB` (`obsidian/tasks/markdown_db.py`) describes the retired task
master-list projection. `ProjectMarkdownDB` (`projects/markdown_db.py`) parses
the legacy one-file-per-project layout for pre-seal import/reconciliation.
Neither is a native product authority, and their old vault-directory settings
are not setup requirements for database-only domains.

## Mass-delete circuit-breaker

"Markdown is canonical" means a store row absent from the parse is soft-deleted — correct only if the parse is trustworthy. `reconcile_drift` refuses any single-pass orphan-delete batch larger than `max(20, 50% of the live store)`, so a degraded read (a bridge blink, a partial/truncated read, a parser/ID-scheme regression) can never silently wipe the store. A refusal skips ONLY the delete loop (creates + field-drift still run), sets `ReconcileReport.aborted_bulk_delete = (would_delete, live_count)`, logs at ERROR, and surfaces as a `task_sync` status of `"degraded"` (not the healthy `"ok"`/`"synced"`). The next reconcile retries with hopefully-sane input.

## Status

Frozen compatibility code. Native Task authority does not call
`TaskMarkdownDB`. Projects may use `ProjectMarkdownDB` only before their
SQLite authority seal; the authority-aware wrapper returns `disabled` with
zero writes after seal. Scheduled reconciliation is retired once the live
cohort is certified. The `lww_meta` tables remain historical audit data and
travel with their domain backups, but they do not authorize dual writes.
