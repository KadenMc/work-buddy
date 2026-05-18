---
name: Chrome Route To Umbrella Task
kind: capability
description: Create a single task representing the whole Chrome group. The cluster label becomes the task text; the tabs are listed in the linked summary note.
capability_name: chrome_route_to_umbrella_task
category: context
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
    description: Project slug for the task
    required: false
  title_override:
    type: str
    description: Override the task text; defaults to the cluster label
    required: false
mutates_state: true
retry_policy: manual
tags:
- context
- chrome
- route
- to
- umbrella
- task
aliases:
- create umbrella task from chrome group
- single task for tab group
parents:
- context
- context
requires:
- obsidian
---
