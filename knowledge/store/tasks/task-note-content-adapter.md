---
name: Task-note Content Adapter
kind: system
description: Authority-aware compatibility seam for linked task-note Markdown bodies; preserves note_uuid links and the task master list while separately gating Co-work authority.
summary: Read or mutate task-note bodies through work_buddy.task_notes.TaskNoteContentAdapter. Never infer content authority from a rollout flag or bypass the adapter with direct tasks/notes/<uuid>.md I/O. The task master list remains authoritative and unchanged.
tags:
- tasks
- task-note
- cowork
- migration
- authority
aliases:
- task note adapter
- task-note migration
- linked task note content
- task note Co-work authority
parents:
- tasks
---

## Boundary

Only the body linked by stable `note_uuid` is in this migration. Task master
rows, scheduling/state metadata, and `[[note_uuid|📓]]` links remain in the
existing Tasks/Obsidian pipeline.

Production task-note body readers and writers must use
`work_buddy.task_notes.TaskNoteContentAdapter`. This includes task mutation and
read payloads, IR, context drill-down, density/backfill helpers, and
email-to-task-note append. Path-shaped provenance/observability detectors may
continue recognizing `tasks/notes/<uuid>.md` because they do not read or mutate
content.

## Authority

Authority is per note UUID and epoch:

1. `legacy_authoritative`
2. `shadow_imported`
3. `cowork_authoritative`
4. `retired`

Task-note cutover is disabled by default. It requires exact file-origin Source
capture with unknown authorship, a bound shadow document, recorded parity, the
Journal migration exit gate, the task-note cutover gate, and a bounded rollback
deadline. A feature/cohort flag is eligibility, never authority.

## Projection safety

After cutover, Markdown is an externally editable compatibility projection.
Projection uses base hash + generation + document head and pauses on any
unexpected base. The changed file is captured exactly as a Source and is not
overwritten. Rollback fences the Co-work epoch and invalidates prepared stale
projection intents.

## Source-backed writes

Bound reads, exact shadow import, cutover/rollback, projection, divergence
capture, retirement, and whole-document replace now share the authority seam.
After cutover, whole-document replace first captures the resulting exact
Markdown as a Source, reserves and rechecks its use, commits a durable
document-kernel change receipt, persists the reverse managed-copy dependency,
acknowledges the use, and projects without clobbering an unexpected Markdown
base. An interrupted operation resumes by idempotency key. Arbitrary append
under Co-work authority remains fail-closed until it has the same crash-safe
source/change receipt contract; it must not fall back to a direct file or
document write.
