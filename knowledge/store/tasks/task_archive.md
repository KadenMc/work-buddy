---
name: Task Archive
kind: capability
description: Move completed tasks from the master list to tasks/archive.md. Consent-gated; the prompt shows the exact count and a random 5-title sample so the user approves a concrete scope. Default policy archives tasks completed >= 7 days ago so recent work stays visible. Posts a fire-and-forget summary notification after the move so a bulk archive doesn't happen silently.
capability_name: task_archive
category: tasks
parameters:
  older_than_days:
    type: int
    description: Only archive tasks done N+ days ago (default 7 = leave the last week in master list as a 'recently done' buffer; pass 0 to archive every completed task regardless of age)
    required: false
tags:
- tasks
- task
- archive
aliases:
- archive done tasks
- clean up completed tasks
- move completed to archive
- task cleanup
- archive old tasks
- tidy task list
parents:
- tasks
- tasks
requires:
- obsidian
---
