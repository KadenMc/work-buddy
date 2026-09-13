---
name: Project Revisions List
kind: skill
description: Return revision history for a project, newest first. Each entry snapshots the project state plus folder + alias sets at that revision.
category: projects
op: op.wb.project_revisions_list
schema_version: wb-skill/v1
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
  limit:
    type: int
    description: Max revisions to return (default 20)
    required: false
skill_name: project_revisions_list
tags:
- projects
- project
- revisions
- list
aliases:
- project history
- project revisions
- project changes
- project audit
- revision history
parents:
- projects
---
