---
name: Project Remove Alias
kind: capability
description: Detach an alias from a project. Writes a revision.
capability_name: project_remove_alias
category: projects
op: op.wb.project_remove_alias
schema_version: wb-capability/v1
parameters:
  slug:
    type: str
    description: Canonical project slug or alias
    required: true
  alias:
    type: str
    description: Alias to remove (matched case-insensitively)
    required: true
  author:
    type: str
    description: '''user'' (default) or ''agent'''
    required: false
  change_summary:
    type: str
    description: Optional one-line summary
    required: false
mutates_state: true
retry_policy: manual
tags:
- projects
- project
- remove
- alias
aliases:
- remove project alias
- drop alias
- unregister alternative project name
parents:
- projects
---
