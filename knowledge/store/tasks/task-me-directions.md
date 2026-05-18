---
name: Task Me Directions
kind: directions
description: How to run /wb-task-me — the re-runnable engage flow that answers what should I do right now
summary: 'Re-runnable. Loads context, builds a clamp-to-now plan, presents top 1-2 recommendations. Slice-5a context-aware: skips tasks the agent and user both can''t satisfy now.'
trigger: user runs /wb-task-me or asks what to work on right now
command: wb-task-me
workflow: task-me
capabilities:
- task_me
tags:
- tasks
- engage
- today
- slice-5b
aliases:
- task me
- engage
- now plan
- what should I do
parents:
- tasks
---

Run mcp__work-buddy__wb_run("task-me") and follow the conductor.

The workflow has 4 steps:

1. **load-context** (auto_run) — composes:
   - task_briefing (focused / mit / overdue / inbox / stale buckets)
   - calendar (today's events; best-effort)
   - contract_constraints (WIP limits + active contracts)
   - the Slice-5a engage view (per-task tier x who_can_act x user_now)

2. **build-now-plan** (auto_run) — generates a clamp-to-now timeline
   from focused tasks + calendar. Uses the engage view's filtered
   set so context-blocked tasks don't land in the plan.

3. **engage** (you, reasoning) — present:
   - Top contract-constraint banner (if any active contracts)
   - ONE recommendation card with rationale traced to the active
     contract OR explicitly labeled "exploration"
   - ONE alternative if a sensible second exists
   - The plan as a compact list (time block + task)

   Keep this brief. V1a — attention scarcity. No more than 2 cards.
   If everything is blocked on missing contexts, name the contexts
   and link to /setup.

4. **write-back** (you, reasoning, OPTIONAL) — only if the user
   explicitly asks to write the plan into the journal Day Planner.
   Calls day_planner_generate_and_write. Skip silently if not asked.

## Args

The slash command may be passed an optional preset:
-  — default; assumes filesystem + vault + web + workstation
- 
- 

These map to the user_current_contexts list. If unset, the workflow
runs without filtering — every task surfaces with its blocker badge.

## Re-runnable

Designed to be called multiple times per day. Each run is independent;
no state is mutated unless the user opts into write-back.

## Don'ts

- Don't recommend more than 2 actions
- Don't silently write to the journal
- Don't treat exploration as paper progress (per CLAUDE.local.md)
- Don't suggest experiments without an active contract
