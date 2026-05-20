---
name: Weekly Review Directions
kind: directions
description: How to run the weekly task review — MIT drafting, WIP enforcement, constraint validation
summary: Agent assembles the picture and proposes; user validates. Under 15 minutes. Every MIT must trace to a contract constraint (max 1 exception). WIP violations require explicit renegotiation.
trigger: user wants to run their weekly task review and planning session
command: wb-task-review
workflow: tasks/weekly-review
capabilities:
- tasks/task_change_state
- tasks/task_briefing
- contracts/contract_constraints
tags:
- tasks
- weekly
- review
- planning
- contracts
- directions
aliases:
- weekly review
- task review
- weekly planning
- strategic review
parents:
- tasks
---

Start via mcp__work-buddy__wb_run("weekly-review"). Follow the conductor.

## Drafting the plan

1. Review last week's MITs -- which completed? Which didn't? Why?
2. Check each active contract's constraint -- has it changed? Is it still the real bottleneck?
3. Propose 3-5 MITs as implementation intentions:
   Format: "When [time/trigger], I will [specific action] for [contract]"
   Each MIT must trace to a contract constraint. Non-contract tasks get max 1 slot.
4. Flag issues: WIP violations, no constraint set, snoozed >14 days, inbox needing promotion/kill
5. Propose state changes: completed -> done, incomplete -> demote/carry, new -> promote

## Validation rules

- Every MIT traces to a contract constraint (max 1 exception for admin/personal)
- WIP violations require explicit renegotiation -- do NOT silently allow
- Constraint changes must be specific, not vague

WIP enforcement: If exceeding limit:
1. State current active contracts
2. Ask which to pause or complete first
3. Record the reasoning

## Don'ts
- Don't present all 50+ tasks -- focus on MITs and flagged items only
- Don't recommend system changes
- Don't spend more than 15 minutes
- Don't create MITs that don't trace to a contract constraint
- Don't let WIP violations pass without renegotiation
- Don't make strategic decisions for the user -- propose, don't decide
