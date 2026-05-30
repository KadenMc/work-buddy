---
name: Task Note Readers
kind: capability
description: Sessions whose transcripts show they READ a task's note — the inverse of developed-by. Detects native Read of tasks/notes/<uuid>.md plus task_read/task_assign MCP calls carrying the task id. Surfaces the Rung-3 "read it, did the work, never referenced the task id in a commit" developer that developed-by misses. Read-only; full-history JSONL scan with a durable-table fast path.
capability_name: task_note_readers
category: tasks
op: op.wb.task_note_readers
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: true
  note_uuid:
    type: str
    description: The task's note UUID. Enables Read-tool detection on tasks/notes/<uuid>.md; without it only the task_read/task_assign MCP-call signals fire. Get it from the task record's note_uuid field.
    required: false
  include_saw_id:
    type: bool
    description: When true, also return sessions that merely mention the task id without an explicit read (weak signal). Default false — only explicit-read sessions are returned.
    required: false
tags:
- tasks
- task
- provenance
- sessions
- note-read
- awareness
- rung-3
aliases:
- who read this task
- who read the note
- sessions that read this task
- note readers
- did anyone read this task
parents:
- tasks
requires: []
---

Returns the sessions whose JSONL transcripts demonstrate they *read* a
task's note — the inverse of `task_provenance`'s `developed_by`.

Where `developed_by` answers "who *committed* against this task id,"
`task_note_readers` answers "who *read the note*." That gap is the Rung-3
"forgot to toggle" case: a session reads the note, does the work, but
never references the task id in a commit message — invisible to
`developed_by`, yet exactly the developer `/wb-task-completeness` wants to
find.

Each returned session carries:

- **awareness** — `read_note` (an explicit read fired) or, only when
  `include_saw_id=true`, `saw_id` (the id appeared without a demonstrable
  read).
- **sources** — which explicit-read signals fired, each with first/last
  timestamps and a count: `read_tool` (a `Read` on
  `tasks/notes/<uuid>.md`), `task_read_mcp`, `task_assign_mcp`.
- **first_seen / last_seen** — ISO timestamps bounding the reads.

Results are ranked `read_note` before `saw_id`, then most-recent first.
Pass `note_uuid` for full fidelity (it's what enables `read_tool`
detection). Bridge-independent — reads session JSONL + the durable
`session_task_note_reads` table (when populated, used as an O(1) fast
path), never the Obsidian bridge.
