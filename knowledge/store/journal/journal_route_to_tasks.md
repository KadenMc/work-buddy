---
name: Journal Route To Tasks
kind: capability
description: 'Walk a journal-group thread''s context items and create one task per item in the master task list. Each item''s label becomes the task text. Continue-on-error: a single failed item doesn''t block the rest.'
capability_name: journal_route_to_tasks
category: journal
parameters:
  thread_id:
    type: str
    description: Group sub-thread to route
    required: true
  vault_root:
    type: str
    description: Override the configured vault root
    required: false
  urgency:
    type: str
    description: low | medium (default) | high
    required: false
  project:
    type: str
    description: Project slug applied to every created task
    required: false
mutates_state: true
retry_policy: manual
tags:
- journal
- route
- to
- tasks
aliases:
- create tasks from journal group
- route group to task list
- spin out group as tasks
parents:
- journal
- journal
requires:
- obsidian
---
