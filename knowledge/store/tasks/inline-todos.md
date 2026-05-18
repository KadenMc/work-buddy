---
name: Inline Todos
kind: workflow
description: Find `#wb/TODO` markers across the vault, present them for triage, execute the freeform instructions, and clean up handled tags.
workflow_name: inline-todos
execution: main
allow_override: false
steps:
- id: discover
  name: Discover inline TODOs across vault
  step_type: code
  depends_on: []
  invokes: []
- id: triage
  name: Present TODOs and collect user decisions
  step_type: reasoning
  depends_on:
  - discover
  invokes: []
- id: execute
  name: Execute instructions for selected TODOs
  step_type: reasoning
  depends_on:
  - triage
  invokes:
  - task_create
- id: cleanup
  name: Clean up handled TODO tags
  step_type: code
  depends_on:
  - execute
  visibility:
    mode: none
  invokes: []
tags:
- tasks
- inline
- todos
parents:
- tasks
---

## discover

No gateway capability exists yet for inline TODO discovery â€” use Python directly:

```python
from work_buddy.obsidian.inline_todos import discover_inline_todos

result = discover_inline_todos(limit=100)
```

If `result["count"] == 0`, report "No #wb/TODO items found in the vault." and skip remaining steps.

Return the result as step output.

## triage

Agentic step. The agent presents TODO items and collects user decisions. Behavioral instructions (batch size, presentation format, action options) are in the slash command, not here.

Collect decisions as structured output:
```json
[
  {"index": 0, "action": "handle"},
  {"index": 1, "action": "skip"},
  {"index": 2, "action": "delete_tag"}
]
```

If all items are skipped, report that and end the workflow.

## execute

Agentic step. The agent interprets and acts on freeform instructions for handled items. Behavioral instructions (interpretation rules, confirmation requirements) are in the slash command, not here.

Report what was done for each handled item.

## cleanup

Build the list of items to clean up (those with `action == "handle"` or `action == "delete_tag"`). No gateway capability exists yet â€” use Python directly:

```python
from work_buddy.obsidian.inline_todos import cleanup_handled_todos

result = cleanup_handled_todos(items_to_clean)
```

> **Note:** For task creation within the execute step, use `mcp__work-buddy__wb_run("task_create", {...})` rather than importing task mutation functions directly.

Report the cleanup summary:
- N items cleaned across M files
- Any skipped items (due to file edits during the workflow)
- Any errors
