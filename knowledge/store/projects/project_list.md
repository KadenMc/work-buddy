---
name: Project List
kind: capability
description: List projects with folders + aliases, ordered by lifecycle status. Soft-deleted rows are filtered by default; pass include_deleted=True to see them.
capability_name: project_list
category: projects
parameters:
  status:
    type: str
    description: 'Filter by status: active, paused, past, future, deleted'
    required: false
  include_deleted:
    type: bool
    description: Include rows with status='deleted' (default False)
    required: false
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
- projects
---
