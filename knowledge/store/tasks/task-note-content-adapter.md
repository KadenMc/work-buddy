---
name: Retired Task-note Content Adapter
kind: system
description: Retired pre-cutover compatibility seam for legacy task-note Markdown; never current task authority.
summary: The adapter is retained for frozen legacy import and recovery only. Native task knowledge lives in projection-free Co-work documents.
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

# Retired compatibility boundary

`work_buddy.task_notes.TaskNoteContentAdapter` describes the earlier per-note
Markdown migration seam. It remains importable only so frozen pre-cutover data,
historical receipts, and rollback evidence can be inspected. It is not a
production task read or write path after native task authority activates.

Current task metadata lives in `TaskStore`. Current task knowledge lives in a
Co-work document with `projection_mode=none`. No task operation projects that
document to Markdown, reads `tasks/notes/<uuid>.md` as current truth, or falls
back to an Obsidian writer. Path-shaped observability may continue recognizing
legacy note reads as historical provenance, but that evidence does not confer
content authority.

See `tasks/native-task-system` for the active architecture and
`tasks/task-note-migration-operator` for the legacy-only operator boundary.
