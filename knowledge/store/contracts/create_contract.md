---
name: Create Contract
kind: skill
description: Create one revisioned contract in the sealed Contracts SQLite authority after the user confirms the draft.
category: contracts
parameters:
  payload:
    type: str
    description: JSON object containing title, type, status, dates, commitments, constraints, evidence links, and body roles.
    required: true
  client_mutation_id:
    type: str
    description: Stable unique identity reused only when retrying this exact confirmed creation request.
    required: true
mutates_state: true
retry_policy: verify_first
consent_operations:
- contracts.create
op: op.wb.create_contract
schema_version: wb-skill/v1
skill_name: create_contract
tags:
- contracts
- create
parents:
- contracts
requires:
- contracts
---

Creates a contract only in the sealed Contracts SQLite authority. It never
creates or edits a Markdown file. New contracts default to `draft`; `active`
must reflect the user's explicit decision and remains subject to the native WIP
limit. The service records an immutable revision, actor, idempotency receipt,
structured body roles, and a search outbox event.
