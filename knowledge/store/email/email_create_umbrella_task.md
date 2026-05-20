---
name: Email Create Umbrella Task
kind: capability
description: Create a single task representing the whole email cluster. The cluster label becomes the task text; the linked summary note lists every email's subject + sender + date for context.
capability_name: email_create_umbrella_task
category: email
op: op.wb.email_create_umbrella_task
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
    description: Project slug for the task
    required: false
  title_override:
    type: str
    description: Override the task text; defaults to the cluster label
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
- umbrella
- task
aliases:
- create umbrella task from email cluster
- single task for email group
parents:
- email
requires:
- obsidian
---
