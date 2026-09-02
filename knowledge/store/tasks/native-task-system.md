---
name: Native Task System
kind: system
description: Canonical SQLite task authority with revision-checked mutations, projection-free Co-work knowledge documents, durable cutover safety, and frozen legacy imports.
summary: Native TaskStore and TaskApplicationService own task truth; every task may bind to a Co-work document that never renders Markdown, while the backed-up Obsidian task corpus remains frozen indefinitely.
tags:
- tasks
- native
- cowork
- authority
- migration
- react
aliases:
- native tasks
- task authority
- TaskStore
- task cutover
parents:
- tasks
entry_points:
- work_buddy.tasks.store
- work_buddy.tasks.service
- work_buddy.tasks.runtime
- work_buddy.tasks.migration
- work_buddy.tasks.documents
- work_buddy.dashboard.tasks_api
dev_notes: |-
  Schema v22 carries the authority epoch, collection revision, tasks, structured tags, lifecycle/history, action items, mutation receipts, outbox, document links, local-file handles, document-stage replay-integrity receipts, aggregate-creation intents and participant receipts, field-derivation receipts, and existing-task document-attachment intents. Mutations are compare-and-swap operations keyed by `expected_revision`; the gateway pins a stable `client_mutation_id` before dispatch so response loss is replay-safe. Conflicts return the live task and revision.

  Native activation is guarded by an external, fsynced authority latch written before the SQLite activation CAS. Once native authority has ever activated for a configured database identity, a missing, moved, corrupt, or path-mismatched database fails closed with `TaskAuthorityUnavailable`; compatibility writers must never fall through to Obsidian. A pending latch also protects the crash window between latch creation and database commit.

  MCP task ops register deterministic authority-routed callables without probing the latch while the registry is being built. Each invocation resolves the current authority. Frozen pre-cutover task-create effect metadata stays static for uncertain legacy-write recovery, but its lazy resolver checks mutation authority before importing or calling the retired Markdown resolver; native retry guards reject those effects earlier.

  The task Co-work store ID is persisted in `task_system_state` and resolved through the Truth store registry so relocation preserves identity. Task reads and IR indexing project the current structured head plus uncompacted Yjs updates. Do not read only the last compacted blob.

  Aggregate task-plus-document creation and existing-task document attachment are separate recoverable sagas. The former reserves task identity before scoped-store work and publishes the task only after the prepared document is admitted against the coordinator decision. The latter persists its complete intent before document reservation and links under the expected task revision. Restoring a task whose binding was retired preserves the retired document and binding, creates one new projection-free successor with retained content, and atomically attaches it at the current task revision.

  Reverse export intentionally splits legacy representability by liveness. Live rows must have parser-safe single-line descriptions and complete note-link identity because they render into master/archive Markdown. Deleted rows are database-only v11 tombstones: preserve nullable descriptions and dangling but syntactically valid note UUIDs in SQLite, while still validating task IDs and any document links that do exist. Local-file handles read from projected Co-work Markdown must be canonicalized for Markdown-escaped punctuation (for example, `lf\_...`) before catalog matching and replacement; verified assets are then rehydrated only from the sealed frozen root.
---

# Native task authority

`TaskStore` and `TaskApplicationService` are the sole source of truth for live task identity, fields, state, completion, archive/trash lifecycle, structured tags, action items, history, provenance, and document links. Obsidian, `tasks/master-task-list.md`, `tasks/archive.md`, task-note Markdown, and the Obsidian Tasks plugin are not runtime readers, writers, mirrors, or reconciliation peers after activation.

Every mutation returns the task ID, task revision, collection revision, and a durable mutation receipt. Callers should pass the current `expected_revision` and may pass a stable `client_mutation_id`; retries with the same semantic request replay, while stale revisions produce a structured conflict instead of overwriting newer work. Completion accepts an optional historical `done_date`; snoozing uses the explicit `snooze_until` field.

Batch creation validates every accepted row before opening its transaction, then
rechecks all task IDs against existing task rows and non-aborted aggregate
creation reservations under the same write lock. Any collision aborts the whole
batch before an accepted task is published.

## Atomic creation and attachment

A scalar-only task is created in one `TaskApplicationService` transaction. When
creation includes a Co-work knowledge document,
`TaskAggregateCreationService` and `TaskCreationCoordinator` own a recoverable
cross-store state machine:

1. validate the complete task request before reserving cross-store state;
2. atomically reserve the explicit or deterministic task ID in
   `task_creation_intents`;
3. prepare the projection-free document, provenance state, requested Truth
   policy, and pending local admission;
4. freeze every participant receipt into the coordinator decision and admit the
   document against that decision;
5. publish the task row, document link, and field-derivation receipts together.

Ordinary task readers cannot see a partial aggregate because the task row does
not exist until publication. Replaying the same client mutation resumes the
same participants, and reconciliation rolls recoverable intents forward without
duplicating a task, document, binding, admission, or receipt.

Attaching a first document to an existing task uses a separate durable intent.
It validates task existence, liveness, revision, and the one-document limit
before the first scoped-store reservation, then links and admits the prepared
document under the recorded expected task revision. Recovery resumes the same
deterministic document and binding identities.

## Co-work knowledge documents

Task notes are ordinary Co-work knowledge documents with `projection_mode=none`. They are not Markdown files and are never rendered back into the vault. Browser edits, task reads, email append, IR parsing, excerpts, and restore all consume the current structured/Yjs head. A task can also hold opaque links to local files in place: the browser receives display metadata and a host action, never an absolute path or copied/encrypted asset unless a separate feature explicitly requests that.

## Migration and frozen legacy data

Migration is backup-first and operator-driven. The inventory includes ID-bearing and ID-less task lines, task metadata, note bodies, attachments, missing references, and unattached notes; the importer assigns deterministic native IDs, creates projection-free Co-work documents, and emits a recovery catalog. Dry-run and parity checks happen before the guarded prepare/fence/activate sequence.

The live cutover is never implicit in application startup, tests, import, or dashboard use. The backed-up legacy task files and database snapshot remain frozen and retained indefinitely until the user explicitly decides otherwise. No 30-day cleanup or automatic deletion is permitted.

## Secondary consumers

Task context, project tag counts, Obsidian context summaries, Chrome/email/Journal routing, completeness, search/IR, MCP capabilities, and both dashboard entry points query the native domain after activation. The disabled `sidecar_jobs/task-sync.md` file remains only as historical configuration.
