---
name: Task Archive
kind: capability
description: Archive eligible completed native tasks by lifecycle flag without moving or rendering Markdown. Default policy leaves the last 7 days visible.
capability_name: task_archive
category: tasks
op: op.wb.task_archive
schema_version: wb-capability/v1
mutates_state: true
retry_policy: manual
parameters:
  older_than_days:
    type: int
    description: Only archive tasks done N+ days ago (default 7 = leave the last week in master list as a 'recently done' buffer; pass 0 to archive every completed task regardless of age)
    required: false
consent_operations:
- tasks.archive
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
requires: []
---
