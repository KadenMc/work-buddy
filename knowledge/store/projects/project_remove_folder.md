---
name: Project Remove Folder
kind: capability
description: Detach a folder from a project. Writes a revision.
capability_name: project_remove_folder
category: projects
op: op.wb.project_remove_folder
schema_version: wb-capability/v1
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
  path:
    type: str
    description: Absolute system path of the folder to remove
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
- folder
aliases:
- remove folder from project
- detach folder
- unregister project folder
parents:
- projects
---
