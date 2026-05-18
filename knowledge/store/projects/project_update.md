---
name: Project Update
kind: capability
description: 'Update a project''s identity: name, status, or description. Writes a revision row capturing the change (author + summary).'
capability_name: project_update
category: projects
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
  name:
    type: str
    description: New human-readable name
    required: false
  status:
    type: str
    description: 'New status: active, paused, past, future, deleted'
    required: false
  description:
    type: str
    description: What is this project? (versioned via revisions)
    required: false
  author:
    type: str
    description: 'Author of this change: ''user'' (default) or ''agent'''
    required: false
  change_summary:
    type: str
    description: Optional one-line summary of what changed
    required: false
mutates_state: true
retry_policy: manual
tags:
- projects
- project
- update
aliases:
- rename project
- change project status
- describe project
- pause project
- archive project
parents:
- projects
- projects
---
