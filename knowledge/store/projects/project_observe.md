---
name: Project Observe
kind: capability
description: Record an observation about a project — strategic decisions, supervisor feedback, pivots, blockers, or anything that shapes trajectory but wouldn't appear in code or tasks
capability_name: project_observe
category: projects
parameters:
  project:
    type: str
    description: Project slug or alias
    required: true
  content:
    type: str
    description: The observation — what happened, what it means, what changed
    required: true
mutates_state: true
retry_policy: manual
tags:
- projects
- project
- observe
aliases:
- observe project
- project note
- project update
- record decision
- project pivot
parents:
- projects
- projects
---
