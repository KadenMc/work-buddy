---
name: Truth Sweep
kind: capability
description: Run and record an integrity, supersession-dependency, or source-dependency sweep for one Truth store.
capability_name: truth_sweep
category: truth
op: op.wb.truth_sweep
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  kind:
    type: str
    description: integrity, supersession, or source.
    required: true
  claim_id:
    type: str
    description: Superseded root claim for a supersession sweep.
    required: false
  evidence_id:
    type: str
    description: Evidence root for a source sweep. Supply exactly one source id.
    required: false
  span_id:
    type: str
    description: Evidence span root for a source sweep. Supply exactly one source id.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- sweep
- integrity
- dependency
aliases:
- sweep truth store
- check truth integrity
- trace claim dependencies
- find stale evidence claims
- audit evidence ledger
parents:
- truth
---
