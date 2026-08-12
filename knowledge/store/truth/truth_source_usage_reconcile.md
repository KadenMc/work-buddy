---
name: Truth Source Usage Reconcile
kind: capability
description: Recover Sources acknowledgement receipts for Truth mutations that committed before a process interruption.
capability_name: truth_source_usage_reconcile
category: truth
op: op.wb.truth_source_usage_reconcile
schema_version: wb-capability/v1
parameters:
  store_id:
    type: string
    description: Optional exact Truth store ID; omit to inspect every reachable store.
    required: false
  limit_per_store:
    type: int
    description: Maximum pending usage receipts to reconcile per store (1-1000, default 100).
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- sources
- reconciliation
- crash-recovery
aliases:
- reconcile truth source usages
- recover truth source receipts
- repair pending truth source acknowledgement
- drain truth source recovery
parents:
- truth
---

Truth and Sources are separate durable authorities. A process may stop after a
Truth transaction commits but before the corresponding Sources usage is
acknowledged. This capability resumes only those exact, already-recorded usage
receipts. It does not re-resolve source content, create another claim, or infer
that a redacted source is clean.
