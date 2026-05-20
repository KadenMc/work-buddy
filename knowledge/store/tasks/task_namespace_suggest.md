---
name: Task Namespace Suggest
kind: capability
description: Rank existing namespace tags by relevance to a task text (hybrid BM25+embedding via the shared embedding service; falls back to token overlap). Returns ranked candidates from the existing universe only — it does not propose new namespaces. The calling agent decides whether to apply suggestions, add more, or mint a new namespace.
capability_name: task_namespace_suggest
category: tasks
op: op.wb.task_namespace_suggest
schema_version: wb-capability/v1
parameters:
  task_text:
    type: str
    description: The task description to score against
    required: true
  contract:
    type: str
    description: Optional contract slug for boosting related namespaces
    required: false
  project:
    type: str
    description: Optional project slug for boosting related namespaces
    required: false
  limit:
    type: int
    description: Max suggestions (default 3)
    required: false
tags:
- tasks
- task
- namespace
- suggest
aliases:
- suggest task tags
- propose namespace
- which namespace fits this task
- tag suggestion
parents:
- tasks
---
