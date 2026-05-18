---
name: Task Me — what should I do right now?
kind: workflow
description: What should I do right now? Loads tasks, calendar, and active contracts; clamps the day plan to the current moment; surfaces 1–2 next-action recommendations; and optionally writes the resulting plan back to the journal Day Planner.
workflow_name: task-me
execution: main
allow_override: false
steps:
- id: load-context
  name: Load task / calendar / contract / engage context
  step_type: code
  depends_on: []
  invokes: []
  auto_run:
    callable: work_buddy.task_me.load_context_for_task_me
- id: build-now-plan
  name: Build clamp-to-now plan from focused tasks + calendar
  step_type: code
  depends_on:
  - load-context
  invokes: []
  auto_run:
    callable: work_buddy.task_me.build_now_plan
    input_map:
      context: load-context
  visibility:
    mode: summary
- id: engage
  name: Recommend top 1-2 actions and surface the plan
  step_type: reasoning
  depends_on:
  - build-now-plan
  invokes: []
- id: write-back
  name: 'Optional: write the plan into journal Day Planner'
  step_type: reasoning
  depends_on:
  - engage
  optional: true
  invokes:
  - day_planner
tags:
- tasks
- engage
- today
- task-me
- slice-5b
parents:
- tasks
---

## load-context

Auto-run: composes task_briefing, calendar, contract_constraints, and the Slice-5a engage view. The engage view filters by user_current_contexts when provided. No mutations.

## build-now-plan

Auto-run: calls work_buddy.obsidian.day_planner.planner.generate_plan with clamp_to_now=True. Returns the proposed timeline; does NOT write back. Pull focused tasks from the engage view (so Slice-5a context filtering applies) — fall back to task_briefing.focused if engage is unavailable.

## engage

Agentic step. Read the engage view in load-context.engage and the proposed plan in build-now-plan.plan. Present:
  1. Top contract-constraint banner (if any active contracts).
  2. ONE recommendation card with rationale traced to the active contract OR labeled exploration.
  3. ONE alternative if a sensible second exists.
  4. The clamp-to-now plan as a compact list.
Keep this brief — V1a (attention scarcity). No more than 2 cards. If everything is blocked on contexts, say so + name the missing contexts + link to /setup.

## write-back

Optional + consent-gated. Only run if the user explicitly asked to write the plan into the journal. Calls day_planner_generate_and_write. Do not run silently. Skip when not asked.
