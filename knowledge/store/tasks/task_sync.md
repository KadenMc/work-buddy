---
name: Task Sync
kind: capability
description: Retired legacy Markdown reconciliation surface; unavailable under native task authority.
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
requires: []
---

Do not call this capability after native cutover. `TaskStore` is already
canonical, and `sidecar_jobs/task-sync.md` is disabled indefinitely. The frozen
legacy files are import and rollback evidence, not a reconciliation peer.
