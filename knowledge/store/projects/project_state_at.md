---
name: Project State At
kind: skill
description: Reconstruct a project's state as of a given timestamp (latest revision ≤ timestamp). Includes folders + aliases as they were then.
category: projects
op: op.wb.project_state_at
schema_version: wb-skill/v1
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
  timestamp:
    type: str
    description: ISO 8601 UTC timestamp (e.g. '2026-04-14T00:00:00Z')
    required: true
skill_name: project_state_at
tags:
- projects
- project
- state
- at
aliases:
- project at time
- project state on date
- historical project state
- point-in-time project
parents:
- projects
---
