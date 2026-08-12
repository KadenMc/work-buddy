---
name: Truth Claim Propose From Conversation
kind: capability
description: Propose an unconfirmed Truth claim supported by one exact durable Work Buddy conversation message.
capability_name: truth_claim_propose_from_conversation
category: truth
op: op.wb.truth_claim_propose_from_conversation
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Exact registered Truth store identity that should receive the proposed claim.
    required: true
  conversation_id:
    type: str
    description: Exact durable Work Buddy conversation identity; never a title or text query.
    required: true
  message_id:
    type: str
    description: Exact durable message identity within conversation_id; never message text or ordinal position.
    required: true
  proposition:
    type: str
    description: Human-readable proposition the current agent semantically inferred from the addressed message.
    required: true
  claim_kind:
    type: str
    description: Claim kind allowed by the target Truth store profile.
    required: true
  producer_model:
    type: str
    description: Model identity for the proposing agent. It must match the gateway session manifest when one records a model.
    required: true
  idempotency_key:
    type: str
    description: Caller-stable key for this exact source and semantic proposal; replay returns the original result and changed reuse conflicts.
    required: true
  evidential_effect:
    type: str
    description: Typed evidence effect, normally supports. Defaults to supports.
    required: false
  derivation_relationship:
    type: str
    description: Typed semantic relationship between message and proposition, normally paraphrase. Defaults to paraphrase.
    required: false
  structured:
    type: dict
    description: Optional profile-validated structured claim fields.
    required: false
  scope:
    type: str
    description: Claim scope. Defaults to store.
    required: false
  valid_from:
    type: str
    description: Optional ISO 8601 valid-time start.
    required: false
  valid_to:
    type: str
    description: Optional ISO 8601 valid-time end.
    required: false
  confidence_extraction:
    type: float
    description: Optional extraction confidence from 0 through 1.
    required: false
  relation_diagnostics:
    type: dict
    description: Optional diagnostics for the typed evidence relationship; these do not constitute human review.
    required: false
  producer_call_id:
    type: str
    description: Optional durable identifier for the model call that prepared the proposition.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- conversation
- sources
- provenance
- claim
aliases:
- propose a claim from a chat message
- remember this conversation statement as a candidate fact
- attach conversation evidence to a truth claim
- extract an unconfirmed claim from a message
- source a truth proposal from chat
parents:
- truth
---

This operation captures or reuses the one Sources item identified by the
native `conversation_id` and `message_id`, then atomically records its evidence
snapshot, whole-message exact span, typed evidence relation, source-resolution
receipt, and managed-use receipt alongside a proposed claim. It accepts no
source-message text from the caller. Equal text in another message is a
different occurrence and cannot satisfy the request.

The gateway session determines the semantic agent actor. The capability has no
actor, issuer, tenant, reviewer, or decision parameters that a caller can
spoof. Conversation-source authorship remains whatever the durable provider can
actually establish; a `user` role alone is not treated as proof of human
authorship.

The result is always a proposal, never confirmation. Use
`truth_claim_confirm` separately when a human has reviewed the exact claim and
its receipts. Only that separately gesture-gated transition can make the claim
eligible for the configured Truth-to-Hindsight projection.
