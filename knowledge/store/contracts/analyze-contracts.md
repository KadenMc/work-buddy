---
name: Analyze Contracts
kind: workflow
description: Review all active contracts, check health, and surface issues for the user.
workflow_name: analyze-contracts
execution: main
steps:
- id: load-contracts
  name: Load contracts from directory
  step_type: code
  depends_on: []
  invokes:
  - contract_health
  - active_contracts
  - stale_contracts
  - overdue_contracts
  - contract_constraints
- id: health-check
  name: Run health check and flag issues
  step_type: reasoning
  depends_on:
  - load-contracts
  invokes: []
- id: check-alignment
  name: Check alignment with current work
  step_type: reasoning
  depends_on:
  - health-check
  invokes: []
- id: surface-actions
  name: Surface one next action per active contract
  step_type: reasoning
  depends_on:
  - check-alignment
  invokes: []
- id: handle-no-contracts
  name: Handle case where no contracts exist
  step_type: reasoning
  depends_on:
  - load-contracts
  optional: true
  invokes: []
tags:
- contracts
- analyze
parents:
- contracts
---

## load-contracts

(main, code)

Load contract data via the gateway:

```
health = mcp__work-buddy__wb_run("contract_health")
active = mcp__work-buddy__wb_run("active_contracts")
stale = mcp__work-buddy__wb_run("stale_contracts")
overdue = mcp__work-buddy__wb_run("overdue_contracts")
constraints = mcp__work-buddy__wb_run("contract_constraints")
```

If no contracts exist (health shows total=0), skip to `handle-no-contracts`.

## health-check

(main, reasoning)

Agentic step. The agent reviews the health data and flags issues. Behavioral instructions (health flags, what to surface, phrasing) are in the slash command, not here.

## check-alignment

(main, reasoning)

Agentic step. The agent checks whether current work maps to an active contract. Behavioral instructions (mode naming, alignment categories) are in the slash command, not here.

## surface-actions

(main, reasoning)

Agentic step. The agent identifies one next action per active contract. Behavioral instructions (format, what to identify) are in the slash command, not here.

## handle-no-contracts

(main, reasoning, optional)

Agentic step. The agent handles the case where no contracts exist. Behavioral instructions (response format, tone) are in the slash command, not here.
