---
name: Task-note Migration Operator
kind: capability
description: Inventory task-note bodies and run one conservative shadow, parity, cutover, rollback, or recovery step through the source-backed Co-work migration boundary.
capability_name: task_note_migration_operator
category: tasks
op: op.wb.task_note_migration_operator
schema_version: wb-capability/v1
parameters:
  action:
    type: string
    description: One of inventory, shadow_import, validate_parity, set_gate, cutover, rollback, or recover.
    required: true
  note_uuid:
    type: string
    description: Stable task-note UUID for a one-note action.
    required: false
  rollback_deadline:
    type: string
    description: Future ISO-8601 deadline required for cutover.
    required: false
  gate:
    type: string
    description: task_note_cutover_gate for set_gate. Journal exit is derived from durable Journal migration evidence.
    required: false
  enabled:
    type: bool
    description: Desired gate state for set_gate.
    required: false
  limit:
    type: int
    description: Maximum pending source-backed changes to recover (1-100, default 25).
    required: false
mutates_state: true
consent_operations:
- tasks.task_note_authority_change
retry_policy: manual
auto_retry: false
tags:
- tasks
- task-note
- cowork
- migration
- recovery
aliases:
- migrate one task note
- task note shadow import
- task note cutover
- task note migration inventory
parents:
- tasks
---

This capability migrates only the Markdown body identified by `note_uuid`.
It never moves task-master, status, scheduling, or link authority. Inventory is
content-free. Shadow import captures the exact Markdown file as a Source with
unknown authorship and persists the managed-copy dependency before Source
acknowledgement. Parity uses newline/BOM normalization only.

The task-note cutover gate is closed when the migration store is created.
Journal readiness is not a second mutable Boolean: cutover calls the Journal
domain's current exit-evidence verifier and requires its persisted cohort and
production-callsite digests to still match. This capability cannot manufacture
that evidence. Gate changes, cutover, and rollback carry a separate high-risk
consent gate. Cutover is
per note and additionally requires recorded parity plus a future rollback
deadline. After cutover, whole-document writes are captured as exact Sources,
reserved and rechecked before a durable document change, acknowledged only
after the reverse dependency is durable, and projected without clobbering an
unexpected file base. Recovery resumes receipts by idempotency key.

Source redaction scrubs automatically only while the exact Source-produced
document head remains current and has no direct edits. A changed/mixed head is
marked for review; Hindsight, projections, and compatibility Markdown never
become content authority.
