---
name: Context Projects
kind: capability
description: Active projects with identity, state, and trajectory — synthesized from vault directories, STATE.md files in repos, task tags, git activity, and contracts. Filters the rendered output to active projects by default; pass ``statuses`` to widen.
capability_name: context_projects
category: context
parameters:
  statuses:
    type: list
    description: 'Project lifecycle statuses to include in the rendered bundle. Default: active only. Valid values: active, paused, future, past. Pass ["active", "paused", "future", "past"] to include everything (deleted is never rendered). Filters only the rendered output — every project is still scanned and synced to the registry.'
    required: false
tags:
- context
- projects
aliases:
- projects
- what projects
- active projects
- current work
- project state
- project list
parents:
- context
- context
---
