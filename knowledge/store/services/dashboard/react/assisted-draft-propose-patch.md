---
name: "Assisted Draft Propose Patch"
kind: capability
description: "Propose allowlisted field edits against a consumed frozen form snapshot without acquiring submission authority."
capability_name: "assisted_draft_propose_patch"
category: "assistance"
op: "op.wb.assisted_draft_propose_patch"
schema_version: "wb-capability/v1"
parameters:
  assistant_session_id:
    type: "string"
    description: "Exact assistant session from the worker binding."
    required: true
  conversation_id:
    type: "string"
    description: "Bound conversation ID."
    required: true
  consumer:
    type: "string"
    description: "Bound assistance inbox consumer."
    required: true
  generation:
    type: "string"
    description: "Exact live lease generation."
    required: true
  message_id:
    type: "string"
    description: "Exact snapshot message ID previously consumed through assisted_draft_context_get."
    required: true
  consumption_receipt_id:
    type: "string"
    description: "Exact generation-bound context receipt."
    required: true
  proposal_id:
    type: "string"
    description: "Stable identity for this turn's single coherent advisory patch; reuse on retry."
    required: true
  operations:
    type: "array"
    description: "Allowlisted set/remove operations with path arrays and typed values. No submit, DOM, callback, or domain actions."
    required: true
mutates_state: true
retry_policy: "manual"
auto_retry: false
parents:
- "services/dashboard/react/assisted-drafts"
tags:
- "dashboard"
- "assistance"
- "conversations"
---

Proposes one coherent allowlisted patch per consumed form snapshot using an exact generation-bound receipt and stable proposal identity. The broker derives binding/revision/hash and rejects malformed or unknown operations atomically. Only the mounted host applies uncontested fields and preserves manual edits/Undo; the tool cannot create tasks, schedule jobs or submit forms.
