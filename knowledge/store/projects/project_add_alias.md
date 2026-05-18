---
name: Project Add Alias
kind: capability
description: Attach an alternative slug (alias) to a project. Aliases route to the canonical row across capabilities. Writes a revision.
capability_name: project_add_alias
category: projects
parameters:
  slug:
    type: str
    description: Canonical project slug or alias
    required: true
  alias:
    type: str
    description: Alternative slug (display casing preserved)
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
- add
- alias
aliases:
- add project alias
- alias project
- alternate project name
- register old project name
parents:
- projects
- projects
---
