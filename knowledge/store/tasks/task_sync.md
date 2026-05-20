---
name: Task Sync
kind: capability
description: 'Compare master task list against SQLite store: detect orphans, create missing store records, report checkbox mismatches'
capability_name: task_sync
category: tasks
op: op.wb.task_sync
schema_version: wb-capability/v1
tags:
- tasks
- task
- sync
aliases:
- sync tasks
- reconcile tasks
- task discrepancy
- task watcher
parents:
- tasks
requires:
- obsidian
---
