---
name: Task Provenance
kind: capability
description: The three session↔task provenance roles for one task — created-by (who minted it), assigned (who claimed it), and developed-by (whose commits satisfied it, with note-read awareness + informed/convergent classification). Read-only; developed-by is derived at read time, never stored.
capability_name: task_provenance
category: tasks
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: true
op: op.wb.task_provenance
schema_version: wb-capability/v1
tags:
- tasks
- task
- provenance
- sessions
- developed-by
- created-by
- awareness
aliases:
- who made this task
- who developed this task
- who created this task
- task session roles
- who worked on this task
- developed by which session
parents:
- tasks
requires: []
---

Returns the three structurally-distinct session roles for a task:

- **created_by** — the one session that minted the task (the
  `created_by_session` column; `null` when unrecorded — human/plugin/
  bootstrap/legacy).
- **assigned** — sessions that claimed it via `task_assign` (the
  `task_sessions` table).
- **developed_by** — sessions whose work satisfied it, *derived* on a
  confidence ladder: Rung 1 (assigned **and** a commit references the
  task id), Rung 2 (commit references the id, no assignment). Each entry
  carries an **awareness** grade (did the session demonstrably read the
  note — `read_note` / `assigned` / `saw_id` / `none`) and an
  informed-vs-**convergent** classification.

**Rung 3 is never computed here.** A task whose intent was satisfied
with no structural link (done-differently, no task-id in any commit) is
not detectable structurally — `intent_attribution.computed` is always
`false`, pointing the caller to `/wb-task-completeness` for reasoning.
Bridge-independent (reads SQLite + session JSONL), so it stays callable
when the Obsidian bridge is down.
