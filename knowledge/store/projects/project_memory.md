---
name: Project Memory
kind: capability
description: 'Read from the project memory bank (Hindsight-backed). Modes: ''search'' (semantic recall, optionally scoped to one project), ''model'' (fetch a mental model: project-landscape, active-risks, recent-decisions, inter-project-deps), ''recent'' (latest project memories)'
capability_name: project_memory
category: projects
parameters:
  query:
    type: str
    description: Search query for project memories
    required: false
  mode:
    type: str
    description: search (default), model, or recent
    required: false
  model_id:
    type: str
    description: 'Mental model ID for mode=model (default: project-landscape)'
    required: false
  project:
    type: str
    description: Project slug or alias to scope search (omit for cross-project)
    required: false
  budget:
    type: str
    description: 'Retrieval depth: low, mid (default), high'
    required: false
tags:
- projects
- project
- memory
aliases:
- project recall
- project memory
- project search
- project history
- project decisions
- project landscape
parents:
- projects
- projects
requires:
- hindsight
---
