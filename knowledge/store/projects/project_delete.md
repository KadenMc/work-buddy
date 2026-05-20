---
name: Project Delete
kind: capability
description: Soft-delete a project (set status='deleted'). Row + folders + aliases + revision history are preserved. Consent-gated.
capability_name: project_delete
category: projects
op: op.wb.project_delete
schema_version: wb-capability/v1
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
