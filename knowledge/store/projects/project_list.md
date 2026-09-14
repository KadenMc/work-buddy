---
name: Project List
kind: skill
description: List projects with folders + aliases, ordered by lifecycle status. Soft-deleted rows are filtered by default; pass include_deleted=True to see them.
category: projects
op: op.wb.project_list
schema_version: wb-skill/v1
parameters:
  status:
    type: str
    description: 'Filter by status: active, paused, past, future, deleted'
    required: false
  include_deleted:
    type: bool
    description: Include rows with status='deleted' (default False)
    required: false
skill_name: project_list
tags:
- projects
- project
- list
aliases:
- list projects
- what projects exist
- show projects
- all projects
parents:
- projects
---
