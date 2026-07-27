---
name: Truth Store Create
kind: capability
description: Safely initialize and register one Folder's canonical .wbuddy/cowork Truth store from a complete profile. A store id is minted when neither the request nor profile supplies one.
capability_name: truth_store_create
category: truth
op: op.wb.truth_store_create
schema_version: wb-capability/v1
parameters:
  root:
    type: str
    description: Folder that will own .wbuddy/manifest.yaml and the canonical .wbuddy/cowork sidecar. Initialization refuses unsafe, nested, ambiguous, or pre-existing state without partial mutation.
    required: true
  profile:
    type: dict
    description: Complete Truth profile mapping. The store_id field may be omitted.
    required: true
  store_id:
    type: str
    description: Optional UUID-compatible store identity.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
consent_operations:
- truth.store_create
tags:
- truth
- store
- create
- registry
aliases:
- create truth store
- initialize truth ledger
- register scoped truth
- new evidence store
- make claim store
parents:
- truth
---
