---
name: Journal Sign In
kind: capability
description: 'Read the active Journal profile’s typed fields and optionally write user-supplied values through Sources with optimistic concurrency.'
capability_name: journal_sign_in
category: journal
op: op.wb.journal_sign_in
schema_version: wb-capability/v1
parameters:
  target:
    type: str
    description: 'Date target: ''today'', ''yesterday'', or YYYY-MM-DD. Default: today.'
    required: false
  write_fields:
    type: str
    description: 'JSON object keyed by a unique fieldId or compositionSlotId. Values may be direct, or envelopes with value/disposition, expected_revision, and stated_at. Omit for read-only.'
    required: false
  client_mutation_id:
    type: str
    description: Stable idempotency key for this exact field batch. Reuse only when retrying the same write.
    required: false
mutates_state: true
retry_policy: manual
consent_operations:
- morning.write_sign_in
tags:
- journal
- sign
- in
aliases:
- sign in
- morning check in
- profile fields
- typed check in
- write sign in
parents:
- journal
requires: []
---

This capability resolves the immutable field composition for the target
logical day. It never assumes that any named field exists. The result exposes
each field’s label, value kind,
constraints, prompt, function contract, interaction behavior, current value,
revision, authorship, and Source reference.

Each written field value is retained in Sources before its typed Journal
revision is committed. A caller should send `expected_revision` when changing
an existing value; omitting it means revision zero, which is safe for first
entry and exact retries but cannot overwrite an existing value. Values written
through this conversational sign-in path are attributed to the enrolled user
with the relaying agent session recorded separately.

The `wellness` member is generic and reports declared function contracts. It
does not revive the retired fixed-marker interpreter or invent a trend model
for fields whose profile does not declare one.
