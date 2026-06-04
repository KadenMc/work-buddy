---
name: Dev-Mode Orientation
kind: workflow
description: Forced orientation before dev work — activate dev mode, search the knowledge store for the subsystem being modified, read the code, then declare the prior art found. Only after advancing the step with a non-trivial declaration may the agent proceed with the actual task.
workflow_name: dev-orient
execution: main
allow_override: false
steps:
- id: orient
  name: Search, read, and declare prior art
  step_type: reasoning
  depends_on: []
  result_schema:
    required_keys:
    - units_read
    - files_read
    - wrappers_found
    key_types:
      units_read: list
      files_read: list
      wrappers_found: list
    min_items:
      units_read: 1
      files_read: 1
      wrappers_found: 1
  invokes:
  - mode_toggle
  - agent_docs
tags:
- dev
- orient
- preparation
- discovery
parents:
- dev
---

## orient

(main, reasoning)

Before touching code, orient on the subsystem you are about to modify. The default dev failure mode is acting on general knowledge without consulting the codebase — this workflow exists because agents (including the one that most recently created it) have skipped this step and written wrong code as a result.

## Required actions

1. **Activate dev mode** so `dev_notes` surface in subsequent knowledge queries:

   ```
   mcp__work-buddy__wb_run("mode_toggle", {"mode_id": "dev", "active": true})
   ```

2. **Identify the subsystem** you are about to modify in one short phrase (for your own framing; not submitted as output).

3. **Issue at least one `agent_docs` call** targeting that subsystem at `depth="full"`. Prefer scope browsing if you do not know the exact path:

   - Exact:   `mcp__work-buddy__wb_run("agent_docs", {"path": "architecture/embedding-service", "depth": "full"})`
   - Browse:  `mcp__work-buddy__wb_run("agent_docs", {"scope": "architecture/"})`
   - Search:  `mcp__work-buddy__wb_run("agent_docs", {"query": "<natural-language question>"})`

4. **Open the relevant code files** — not just headers, read them. Identify existing wrappers, classes, or functions that already solve part of the problem. If the subsystem has tests, at least skim their shape.

5. **Advance this step** via `wb_advance` with a dict in this exact shape. Empty or trivial lists are a signal you have not oriented — go deeper and try again:

   ```
   {
     "units_read":     ["<path/to/unit>", ...],
     "files_read":     ["<repo-relative/or/absolute/path>", ...],
     "wrappers_found": ["<existing function/class/capability>", ...]
   }
   ```

## If you are tempted to skip

That is the exact signal you need this step most. Your feeling of already-understanding is how previous dev agents have ended up writing wrong code. The cost of a minute of discovery is trivial against the cost of a wrong commit. Orient anyway.

## What this step does NOT require

- A long prose analysis — the three lists are enough.
- Reading every file in the subsystem — just the ones load-bearing for the change.
- Loading every related knowledge unit — one or two at `depth="full"` is usually the right amount.

Only after advancing with a non-trivial declaration may you return to the user and proceed with the actual dev task.
