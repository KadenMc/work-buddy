---
name: Contract Check Directions
kind: directions
description: How to analyze contracts — health flags, alignment check, per-contract next actions, work-pattern cross-reference
summary: Run five contract MCP calls, flag health issues with specific phrasing, check whether current work maps to a contract, and surface one next action per active contract. Check for related blindspot patterns.
trigger: user wants to review, audit, or check the health of their active contracts
command: wb-contract-check
workflow: contracts/analyze-contracts
capabilities:
- contracts/contract_health
- contracts/active_contracts
- contracts/contract_constraints
- contracts/stale_contracts
- contracts/overdue_contracts
tags:
- contracts
- health
- alignment
- blindspots
- directions
aliases:
- check contracts
- analyze contracts
- contract health review
- contract status
- audit contracts
parents:
- contracts
---

Gather all contract data via the MCP gateway:

mcp__work-buddy__wb_run("contract_health")
mcp__work-buddy__wb_run("active_contracts")
mcp__work-buddy__wb_run("contract_constraints")
mcp__work-buddy__wb_run("stale_contracts")
mcp__work-buddy__wb_run("overdue_contracts")

Then follow the analyze-contracts workflow.

## Health check flags

Review contract_health and flag:
- No active contracts: "You have no active contracts. Are you in exploration mode, or should we define one?"
- No paper contracts: "None of your active contracts are papers. Is that consistent with your publication goal?"
- Multiple active contracts: Check: "You have N active contracts. Is this sustainable, or are you taking on too many at once?"
- Overdue contracts: "Contract X is past its deadline. Should we rescope, extend, or abandon it?"
- Stale contracts: "Contract X hasn't been reviewed in N days. Is it still active?"
- Missing fields: "Contract X is missing a kill rule / draft threshold / deadline."

## Alignment check

If you have context about current work:
- Does the current work map to an active contract?
- If not, is it exploration, admin, or drift?
- Name the mode explicitly: "Your current work appears to be [paper/exploration/admin/drift] relative to your contracts."

## Next actions

For each active contract, identify one line:
- What is the most important incomplete must-have item?
- Is the user blocked on anything?
- What would move the needle most today?

## If no contracts exist

State once and move on: "You have no contracts defined. All current work is implicitly exploration mode." Do NOT nag.

## Related blindspots

During analysis, watch for signals that match the user's documented personal knowledge patterns (knowledge_personal, any category they track). Generic signals to watch:
- Contracts keep getting rescoped
- Must-have lists growing
- Progress stalled but no blocker named
- "It's not enough yet" without citing a threshold
- Work happening but doesn't map to any contract

If a signal matches a pattern the user has documented, name that pattern. Otherwise describe the signal plainly without inventing a label.
