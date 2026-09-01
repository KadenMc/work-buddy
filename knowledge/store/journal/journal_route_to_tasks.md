---
name: Journal Route To Tasks
kind: capability
description: 'Explicit legacy compatibility action that routes an archived journal-group thread into native tasks. Disabled when Obsidian is opted out; new Journal actions use native item routing.'
capability_name: journal_route_to_tasks
category: journal
op: op.wb.journal_route_to_tasks
schema_version: wb-capability/v1
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
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
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
requires:
- obsidian
---
