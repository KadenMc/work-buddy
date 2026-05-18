---
name: Contract Creation Directions
kind: directions
description: How to guide contract creation — interview flow, minimum viable fields, scope checking, WIP awareness, confirmation rules
summary: Walk the user through contract fields interactively. Require a minimum viable set before activation. Ask scope-checking questions. Check against existing contracts to avoid over-commitment. Never activate without explicit user decision.
trigger: user wants to create a new contract for a bounded deliverable
command: wb-contract-new
workflow: contracts/create-contract
capabilities:
- contracts/active_contracts
- contracts/contract_wip_check
tags:
- contracts
- create
- interview
- scope
- directions
aliases:
- create contract
- new contract
- define contract
- start contract
- draft contract
parents:
- contracts
---

Follow the create-contract workflow. Use the template at _template.md in the contracts directory (resolved from contracts.vault_path in config, via get_contracts_dir()).

## Interview flow

Walk the user through fields interactively -- don't try to fill everything at once.

Start with the basics:
- "What are you trying to produce?" (paper, deployment, grant, etc.)
- "What is the central claim or goal in one sentence?"
- "Is there a deadline?"

Minimum viable contract (required before status can be active):
- title, type, claim/goal, deadline (even if rough), at least one must-have evidence item

Fields that can be filled later: kill rule, rescope rule, draft threshold, optional items

Set status: draft initially. Tell the user: "This contract is in draft. Review the must-haves and stop rules, then set it to active when you're ready to commit."

## Scope checking

Before finalizing, ask:
- "Is this scoped to a single claim, or are there multiple claims hiding in here?"
- "Are the must-haves truly must-haves, or could any of them be optional?"
- "What would make you abandon this contract?" (kill rule)
- "What would make you reduce scope?" (rescope rule)

## Review against existing

If other active contracts exist:
- "You already have N active contracts. Does this new one compete for the same time?"
- "Is this a genuine new deliverable, or could it be a branch of an existing contract?"

This catches over-commitment at the contract level.

## Confirm and save

Show the user the complete contract. Get explicit confirmation before saving. Set last_reviewed to today's date. Set status: draft unless the user explicitly says to activate it.

## Don'ts
- Don't create a contract without user involvement -- this is collaborative
- Don't set status to active without the user's explicit decision
- Don't add must-have items the user didn't specify
- Don't make the process feel like bureaucracy -- keep it lightweight
