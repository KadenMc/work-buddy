---
name: Weekly Review
kind: workflow
description: Agentic weekly planning session. The agent assembles the strategic picture,
workflow_name: weekly-review
execution: main
allow_override: false
steps:
- id: gather
  name: Gather strategic state
  step_type: code
  depends_on: []
  visibility:
    mode: summary
    include_keys:
    - contracts
    - wip_status
    - overdue
    - inbox_count
    - stale_count
  invokes:
  - weekly_review_data
  - project_list
- id: draft
  name: Draft weekly plan
  step_type: reasoning
  depends_on:
  - gather
  invokes: []
- id: validate
  name: Validate with user
  step_type: reasoning
  depends_on:
  - draft
  invokes: []
- id: apply
  name: Apply decisions
  step_type: code
  depends_on:
  - validate
  visibility:
    mode: none
  invokes:
  - task_change_state
  - task_toggle
- id: record
  name: Record the plan
  step_type: reasoning
  depends_on:
  - apply
  invokes: []
tags:
- tasks
- weekly
- review
parents:
- tasks
---

## gather

Gather the strategic state via the gateway:

```
data = mcp__work-buddy__wb_run("weekly_review_data")
```

Returns:
- Active contracts with their current constraints
- WIP limit status
- Current MITs (from last week â€” did they get done?)
- Task counts and distribution
- Staleness report
- Inbox tasks with suggested actions
- Overdue tasks
- Constraint-informed focus suggestions

Also fetch the project landscape:
```
projects = mcp__work-buddy__wb_run("project_list")
```

This provides the project list (name, status, observation counts) for the draft step to reference alongside contracts â€” ensuring the review can identify active projects without contracts, and projects with no recent activity.

Also check git log for the past week:
```bash
git log --oneline --since="7 days ago" --all
```

Return all gathered data as the step output.

## draft

Agentic step. The agent drafts a weekly plan from the gathered data. Behavioral instructions (MIT format, constraint checking, issue flagging, state change proposals) are in the slash command, not here.

## validate

Agentic step. The agent presents the draft plan to the user for confirmation or adjustment. Behavioral instructions (WIP enforcement, renegotiation protocol, validation rules) are in the slash command, not here.

Collect all validated decisions as structured output.

## apply

Execute the validated decisions via the gateway:

```
// Promote tasks to MIT
for each new_mit:
    mcp__work-buddy__wb_run("task_change_state", {"task_id": "<id>", "state": "mit", "due_date": "<date>"})

// Complete done tasks
for each completed:
    mcp__work-buddy__wb_run("task_toggle", {"task_id": "<id>", "done": true})

// Demote carried-over MITs back to inbox
for each demoted:
    mcp__work-buddy__wb_run("task_change_state", {"task_id": "<id>", "state": "inbox"})
```

If constraint updates were agreed, update the contract files' `current_constraint` frontmatter field.

## record

Agentic step. The agent records the weekly plan. Behavioral instructions (what to include, brevity guidance) are in the slash command, not here.
