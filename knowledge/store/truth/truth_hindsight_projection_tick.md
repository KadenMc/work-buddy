---
name: Truth Hindsight Projection Tick
kind: capability
description: Reconcile authoritative current Truth claims with the replaceable Hindsight memory projection, then drain a bounded durable outbox batch.
capability_name: truth_hindsight_projection_tick
category: truth
op: op.wb.truth_hindsight_projection_tick
schema_version: wb-capability/v1
parameters:
  store_id:
    type: string
    description: Optional exact Truth store ID. Omit to process every reachable registered store.
    required: false
  limit_per_store:
    type: int
    description: Maximum outbox effects to inspect per store in this tick (1-500, default 20).
    required: false
  reconcile:
    type: bool
    description: Compare authoritative Truth desired state and destination receipts before draining (default true).
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- hindsight
- projection
- reconciliation
- outbox
aliases:
- reconcile truth memory
- drain truth hindsight outbox
- refresh confirmed claim memory
- run truth memory projection worker
parents:
- truth
---

This capability never reads Hindsight as Truth authority. It projects only
current confirmed claims admitted by the configured rollout and claim-support
policy. Exact proposition bytes cross the possibly LLM-backed Hindsight retain
boundary through a run-owned Agent Execution disclosure manifest. Challenge,
supersession, expiry, policy disablement, and redaction produce explicit
idempotent removal intent; ambiguous sends reconcile before any new attempt.

The default rollout is disabled. Enabling
`hindsight.truth_projection.enabled` also requires an explicit authorization
reference plus recipient, provider, and model identifiers. The recurring job is
a low-latency wake-up; durable Truth outbox rows and deterministic
reconciliation are the recovery mechanism.
