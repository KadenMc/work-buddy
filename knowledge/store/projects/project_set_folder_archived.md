---
name: Project Set Folder Archived
kind: capability
description: Flip the archived flag on a project folder (mark dormant or active). Writes a revision.
capability_name: project_set_folder_archived
category: projects
op: op.wb.project_set_folder_archived
schema_version: wb-capability/v1
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
  path:
    type: str
    description: Absolute system path of the folder
    required: true
  archived:
    type: bool
    description: True to archive, False to unarchive
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
- set
- folder
- archived
aliases:
- archive project folder
- unarchive project folder
- mark folder dormant
parents:
- projects
---
