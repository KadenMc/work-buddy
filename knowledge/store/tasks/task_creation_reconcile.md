---
name: Task Creation Reconcile
kind: capability
description: Bounded maintenance owner that rolls hidden task-plus-document creation intents forward after process crashes or response loss.
capability_name: task_creation_reconcile
category: tasks
op: op.wb.task_creation_reconcile
schema_version: wb-capability/v1
parameters:
  limit:
    type: int
    description: Maximum hidden intents to inspect in this run (1-100; default 25).
    required: false
mutates_state: true
retry_policy: verify_first
tags:
- tasks
- recovery
- maintenance
aliases:
- recover pending task creation
- reconcile task document creation
parents:
- tasks
requires: []
---

This is the production crash-recovery owner for aggregate task creation and
existing-task document attachment. It does not create a new user intent. It
resumes only the canonical request and actor already frozen in `TaskStore`,
verifies every prepared participant receipt, and advances the same coordinator
decision idempotently. Existing-task attachment intents are durable before the
document reservation, so maintenance can either win the exact TaskStore link
CAS and commit admission or abort and retire the losing reservation. Ordinary
task reads never expose a new aggregate until TaskStore publication and the
scoped Co-work admission seal are both durable. Conflicting or corrupt state
remains hidden and is reported for operator attention.
