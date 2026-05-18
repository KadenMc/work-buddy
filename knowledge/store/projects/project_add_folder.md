---
name: Project Add Folder
kind: capability
description: Attach a folder to a project. Writes a revision capturing the new folder set.
capability_name: project_add_folder
category: projects
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
  path:
    type: str
    description: Absolute system path to the folder
    required: true
  archived:
    type: bool
    description: Mark folder as archived/dormant (default False)
    required: false
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
- add
- folder
aliases:
- add folder to project
- attach folder
- register project folder
- track project folder
parents:
- projects
- projects
---
