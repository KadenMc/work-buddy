---
name: Truth Hindsight Projection Authorization
kind: capability
description: Preview, grant, inspect, or revoke a narrow expiring authorization for background projection of eligible confirmed Truth into Hindsight.
capability_name: truth_hindsight_projection_authorization
category: truth
op: op.wb.truth_hindsight_projection_authorization
schema_version: wb-capability/v1
parameters:
  action:
    type: string
    description: One of preview, status, grant, or revoke.
    required: true
  store_id:
    type: string
    description: Exact Truth store whose confirmed claims may be projected.
    required: true
  authorization_ref:
    type: string
    description: Existing authorization identity for status/revoke, or an optional caller-stable identity for grant.
    required: false
  policy_id:
    type: string
    description: Exact rollout policy identity; defaults to confirmed_current_v1.
    required: false
  recipient:
    type: string
    description: Exact Hindsight recipient identity for grant.
    required: false
  provider_id:
    type: string
    description: Exact provider identity for grant.
    required: false
  model_id:
    type: string
    description: Exact model identity for grant.
    required: false
  eligible_claim_kinds:
    type: list
    description: Optional bounded claim-kind allowlist; omission uses the policy's all-kinds setting.
    required: false
  projection_method:
    type: string
    description: Exact semantic projection method, normally hindsight_llm_retain_v1.
    required: false
  expires_at:
    type: string
    description: Required ISO-8601 expiry for grant, no more than one year away.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- hindsight
- authorization
- egress
- background
aliases:
- authorize truth memory projection
- revoke hindsight truth access
- inspect truth projection grant
- grant background truth egress
parents:
- truth
---

`enabled: true` and a nonempty configuration string do not authorize egress.
Every upsert must resolve this durable grant and match its store, policy,
recipient, provider, model, claim-kind boundary, method, expiry, and revocation
state immediately before disclosure. Grant and revoke always require a fresh
high-weight consent decision. Removal/reconciliation may continue after
revocation because it does not disclose new claim content.
