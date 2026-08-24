---
name: Task Note Readers
kind: capability
description: Sessions whose transcripts show they read a task through native task/document calls, with legacy Markdown reads retained only as historical evidence.
capability_name: task_note_readers
category: tasks
op: op.wb.task_note_readers
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-xxxxxxxx')
    required: true
  note_uuid:
    type: str
    description: Optional legacy note UUID used only to include pre-cutover Read-tool evidence.
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

Returns sessions whose transcripts demonstrate they read a task or its current
Co-work knowledge — the inverse of `task_provenance`'s `developed_by`.

Where `developed_by` answers "who *committed* against this task id,"
`task_note_readers` answers "who read the task or knowledge." That gap is the Rung-3
"forgot to toggle" case: a session reads the note, does the work, but
never references the task id in a commit message — invisible to
`developed_by`, yet exactly the developer `/wb-task-completeness` wants to
find.

Each returned session carries:

- **awareness** — `read_note` (an explicit read fired) or, only when
  `include_saw_id=true`, `saw_id` (the id appeared without a demonstrable
  read).
- **sources** — which explicit-read signals fired, each with first/last
  timestamps and a count: native `task_read_mcp`, `task_assign_mcp`, Co-work
  document access, or historical `read_tool` evidence for a frozen legacy path.
- **first_seen / last_seen** — ISO timestamps bounding the reads.

Results are ranked `read_note` before `saw_id`, then most-recent first.
Pass `note_uuid` only when pre-cutover path evidence is relevant.
Bridge-independent — reads session JSONL plus the durable
`session_task_note_reads` table (when populated, used as an O(1) fast
path), never the Obsidian bridge. A legacy path read is provenance, not current
content authority.
