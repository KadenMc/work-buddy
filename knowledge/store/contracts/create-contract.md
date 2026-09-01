---
name: Create Contract
kind: workflow
description: Guide the user through defining a new contract for a bounded deliverable.
workflow_name: create-contract
execution: main
allow_override: false
steps:
- id: identify-deliverable
  name: Identify the deliverable
  step_type: reasoning
  depends_on: []
  invokes: []
- id: draft-contract
  name: Draft the structured contract
  step_type: reasoning
  depends_on:
  - identify-deliverable
  invokes: []
- id: check-scope
  name: Check for scope issues
  step_type: reasoning
  depends_on:
  - draft-contract
  invokes: []
- id: review-existing
  name: Review against existing contracts
  step_type: reasoning
  depends_on:
  - draft-contract
  invokes: []
- id: confirm-save
  name: Confirm and create in SQLite
  step_type: reasoning
  depends_on:
  - check-scope
  - review-existing
  requires_individual_consent: true
  invokes:
  - create_contract
tags:
- contracts
- create
- contract
parents:
- contracts
---

## identify-deliverable

(main, reasoning)

Agentic step. The agent interviews the user to identify the deliverable. Behavioral instructions (questions to ask, interview flow) are in the slash command, not here.

## draft-contract

(main, reasoning)

Agentic step. The agent drafts a structured payload for the native Contracts
service. Include a logical-name alias, typed dates, commitments, constraints,
evidence links, and explicit body roles. Do not create or edit a file.

## check-scope

(main, reasoning)

Agentic step. The agent checks for scope issues before finalizing. Behavioral instructions (scope questions, what to challenge) are in the slash command, not here.

## review-existing

(main, reasoning)

Agentic step. The agent reviews the new contract against existing active contracts. Behavioral instructions (competition check, branch detection) are in the slash command, not here.

## confirm-save

(main, reasoning)

Agentic step. The agent presents the complete contract for user confirmation.
After explicit confirmation, call `create_contract` with the structured JSON
payload and a stable mutation identity. Behavioral instructions (confirmation
requirements and status rules) are in the slash command, not here.
