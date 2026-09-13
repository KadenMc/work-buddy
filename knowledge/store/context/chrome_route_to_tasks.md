---
name: Chrome Route To Tasks
kind: skill
description: Walk a Chrome-group thread's tabs and create one task per tab. Each tab's title becomes the task text; the URL goes into a linked summary note.
category: context
op: op.wb.chrome_route_to_tasks
schema_version: wb-skill/v1
parameters:
  thread_id:
    type: str
    description: Chrome group sub-thread to route
    required: true
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
skill_name: chrome_route_to_tasks
tags:
- context
- chrome
- route
- to
- tasks
aliases:
- create tasks from chrome group
- tabs to task list
- spin out tabs as tasks
parents:
- context
---
