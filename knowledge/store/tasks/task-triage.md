---
name: Task Triage
kind: workflow
description: 'Interactive inbox review: surface tasks that need decisions, collect user input, apply state changes.'
workflow_name: task-triage
execution: main
allow_override: false
steps:
- id: gather
  name: Gather task data
  step_type: code
  depends_on: []
  visibility:
    mode: summary
    include_keys:
    - inbox
    - stale
    - counts
  invokes:
  - task_review_inbox
  - task_stale_check
  - task_briefing
  - project_list
- id: present
  name: Present and collect decisions
  step_type: reasoning
  depends_on:
  - gather
  invokes: []
- id: apply
  name: Apply mutations
  step_type: code
  depends_on:
  - present
  visibility:
    mode: none
  invokes:
  - task_change_state
  - task_toggle
- id: summary
  name: Report changes
  step_type: reasoning
  depends_on:
  - apply
  invokes: []
tags:
- tasks
- task
- triage
parents:
- tasks
dev_notes: 'task-triage previously auto-fired `task_archive()` (zero args) at the apply step when the gather step showed >10 completed tasks. This collapsed bulk-archive into the same consent prompt as per-task state changes, leading to the 2026-05-08 incident where 183 tasks archived against a user who thought they were approving a single-task test. The fix: archiving is now an explicit, separate operation. The capability''s own consent body shows a count and a random 5-title sample (see `archive_completed._archive_consent_body_extras`).'
---

## gather

Gather task data via the gateway:

```
inbox_review = mcp__work-buddy__wb_run("task_review_inbox")
stale = mcp__work-buddy__wb_run("task_stale_check")
counts = mcp__work-buddy__wb_run("task_briefing")
projects = mcp__work-buddy__wb_run("project_list")
```

Return the combined results as the step output.

## present

Agentic step. The agent presents tasks and collects user decisions. Behavioral instructions (batch size, presentation format, action options) are in the slash command, not here.

Collect decisions as structured output:
```json
[
  {"description_match": "...", "action": "focus|snooze|done|skip"},
  ...
]
```

## apply

For each decision from the present step, apply mutations via the gateway:

```
for each decision:
    if action == "focus":
        mcp__work-buddy__wb_run("task_change_state", {"task_id": "<id>", "state": "focused"})
    elif action == "snooze":
        mcp__work-buddy__wb_run("task_change_state", {"task_id": "<id>", "state": "snoozed"})
    elif action == "done":
        mcp__work-buddy__wb_run("task_toggle", {"task_id": "<id>", "done": true})
    // "skip" = no-op
```

Do NOT call `task_archive` from this step. Archiving is a separate, consent-gated bulk operation invoked deliberately (e.g. via `/wb-task-triage` follow-up or `wb_run("task_archive")` with explicit intent). The default policy archives only tasks completed >= 7 days ago — older completed work — so a single triage pass shouldn't rake up everything the user just finished. See `tasks/task_archive` for the consent prompt's count + sample-title preview.

## summary

Agentic step. The agent reports what changed. Behavioral instructions (what to include, format) are in the slash command, not here.
