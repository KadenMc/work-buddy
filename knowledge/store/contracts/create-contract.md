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
  name: Draft the contract file
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
  name: Confirm and save
  step_type: reasoning
  depends_on:
  - check-scope
  - review-existing
  invokes: []
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

Agentic step. The agent drafts the contract file using the template at `_template.md` in the contracts directory (resolved via `get_contracts_dir()`).

Create a new file in the contracts directory with a descriptive name (e.g., `my-project-experiment-1.md`, `deployment-pipeline.md`). The contracts directory is in the Obsidian vault, configured by `contracts.vault_path` in config.yaml. Behavioral instructions (minimum viable fields, initial status, what to tell the user) are in the slash command, not here.

## check-scope

(main, reasoning)

Agentic step. The agent checks for scope issues before finalizing. Behavioral instructions (scope questions, what to challenge) are in the slash command, not here.

## review-existing

(main, reasoning)

Agentic step. The agent reviews the new contract against existing active contracts. Behavioral instructions (competition check, branch detection) are in the slash command, not here.

## confirm-save

(main, reasoning)

Agentic step. The agent presents the complete contract for user confirmation and saves it. Behavioral instructions (confirmation requirements, status rules) are in the slash command, not here.
