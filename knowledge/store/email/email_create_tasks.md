---
name: Email Create Tasks
kind: capability
description: Walk an email-cluster thread and create one task per email. The subject becomes the task text; sender + date land in the linked summary note.
capability_name: email_create_tasks
category: email
op: op.wb.email_create_tasks
schema_version: wb-capability/v1
parameters:
  thread_id:
    type: str
    description: Email-cluster sub-thread to route
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
tags:
- email
- create
- tasks
aliases:
- create tasks from email cluster
- emails to task list
- spin out emails as tasks
parents:
- email
requires:
- obsidian
---
