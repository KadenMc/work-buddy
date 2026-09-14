---
name: Project Delete
kind: skill
description: Soft-delete a project (set status='deleted'). Row + folders + aliases + revision history are preserved. Consent-gated.
category: projects
op: op.wb.project_delete
schema_version: wb-skill/v1
parameters:
  slug:
    type: str
    description: Project slug or alias to soft-delete
    required: true
  author:
    type: str
    description: 'Author: ''user'' (default) or ''agent'''
    required: false
mutates_state: true
retry_policy: manual
skill_name: project_delete
tags:
- projects
- project
- delete
aliases:
- delete project
- remove project
- drop project
- unregister project
- soft delete project
- archive project completely
parents:
- projects
---
