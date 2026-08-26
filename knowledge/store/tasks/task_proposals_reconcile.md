---
name: Task Proposals Reconcile
kind: capability
description: Resume already-approved task proposal executions and synchronize durable Journal proposal follow-ups after an interrupted process.
capability_name: task_proposals_reconcile
category: tasks
op: op.wb.task_proposals_reconcile
schema_version: wb-capability/v1
parameters:
  limit:
    type: int
    description: Maximum entries per Threads and Journal stage (1-100, default 50).
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- tasks
- threads
- journal
- reconciliation
- crash-recovery
aliases:
- reconcile task proposals
- recover approved task proposals
- synchronize journal proposal links
parents:
- tasks
---

Threads remains the proposal authority. This bounded maintenance pass resumes
only execution intents with their committed human approval and acceptance
receipt. It reuses `task-proposal:<threadId>` and the original recorded approver
at the standard TaskStore boundary, so a cross-process crash retry preserves
the actor-bound receipt and never creates a second task. It cannot accept a
ready proposal, infer an action, call a model, or change reviewed task fields.

After Threads, Journal replays only durable proposal ingress effects and
synchronizes realization references. Journal does not own another proposal or
task store. Each stage is limited independently, and a failed stage is reported
without stranding the other. Dashboard read-only mode and Source Foundation
restore fencing pause this operation without opening the domain write paths.

The enabled system sidecar job runs on a five-minute schedule with bounded
jitter. Its definition can be overridden through the normal user-job policy.
Deploy its Python implementation with a coordinated sidecar/dashboard restart;
job hot-reload alone does not replace loaded Python code. Explicit human retry
remains available in the proposal UI; GET requests never execute or reconcile
tasks.
