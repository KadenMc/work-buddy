---
name: Truth Claim Challenge
kind: capability
description: Challenge a confirmed claim with a supported, nonterminal conflicting claim while preserving both histories.
capability_name: truth_claim_challenge
category: truth
op: op.wb.truth_claim_challenge
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  claim_id:
    type: str
    description: Confirmed claim being challenged.
    required: true
  challenging_claim_id:
    type: str
    description: Supported live claim that conflicts with the target.
    required: true
  producer_model:
    type: str
    description: Required model claim for the agent authoring this challenge. It must match a non-placeholder session-manifest model; otherwise it is treated as caller_asserted, not authenticated.
    required: true
  note:
    type: str
    description: Optional explanation of the challenge.
    required: false
  producer_call_id:
    type: str
    description: Optional durable model call identifier.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- claim
- challenge
- conflict
aliases:
- challenge truth claim
- flag conflicting fact
- dispute confirmed claim
- open claim conflict
- contest stored truth
parents:
- truth
---
