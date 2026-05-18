---
name: Morning Routine Directions
kind: directions
description: How to run the morning routine — sign-in conversation, blindspot scan, synthesis, propose-mits, persist-briefing, day-planner, quality checks
summary: Start with sign-in (warm, 2-3 messages). Load morning config to check phase enablement. Skip disabled phases. Present 10-15 line briefing. Each MIT traces to a contract constraint.
trigger: user wants to start their morning routine or check in for the day
command: wb-morning
workflow: morning/morning-routine
capabilities:
- context/context_bundle
- journal/journal_sign_in
- tasks/task_briefing
- contracts/active_contracts
tags:
- morning
- routine
- sign-in
- blindspot
- MIT
- briefing
- directions
aliases:
- morning routine
- start the day
- daily check-in
- morning briefing
parents:
- morning
- morning
---

Start via mcp__work-buddy__wb_run("morning-routine"). Load morning config first:

from work_buddy.morning import get_morning_config, is_phase_enabled, resolve_phases
cfg = get_morning_config()

For each step, check is_phase_enabled(step_id, cfg). If disabled, advance immediately with {skipped: true}.

Runtime overrides:
- full-scan: get_morning_config(overrides=["morning.blindspot_depth=full"])
- quick: get_morning_config(overrides=["morning.phases.blindspot_scan=false", "morning.phases.contract_check=false"])

Steps:
1. context-snapshot -- Fresh context collection
2. sign-in -- Morning check-in (sleep, energy, mood, motto)
3. yesterday-close (optional) -- Close yesterday's Log gaps silently
4. calendar-today (optional) -- Today's schedule
5. task-briefing (optional) -- Task status summary
6. contract-check (optional) -- Contract health and constraints
7. blindspot-scan (optional) -- Pattern detection
8. synthesize -- Combine into briefing
9. propose-mits -- Propose MITs, user review, create tasks
10. persist-briefing -- Write briefing to journal (consent-gated)
11. day-planner -- Generate Day Planner schedule (consent-gated)

## Sign-in conversation

Warm, concise, human. This is a conversation, not a form.
- 2-3 messages max. Don't lecture or dump data.
- If sleep/energy/mood missing, ask in one message.
- If motto missing, propose one based on yesterday's patterns.
- Use wellness trends to inform tone but don't dump raw data.

## Blindspot scan -- light mode

<<wb:metacognition/blindspot-directions>>

Return 'None detected' or pattern names with one-line evidence.

## Synthesize guidelines

- Use sign-in wellness context to inform tone.
- Present state, not recommendations for system improvements.
- After briefing, offer interactive follow-ups (triage inbox, process Running Notes).

## Propose-MITs guidelines

- Each MIT traces to a contract constraint. Max 1 exception for admin/personal.
- Each MIT is a concrete, completable action.
- Present proposed MITs for review before creating.
- Include `#tasker/state/focused` in task_text (interim workaround for Dataview visibility, see t-abe6ea4b).

## Persist-Briefing guidelines

- Gated by `persist_briefing` config flag.
- Briefing callout goes in Sign-In section, after Motto, before Tasks & Objectives.

## Day-Planner guidelines

- Gated by `day_planner.enabled` config flag.
- Must check `hasRemoteCalendars` in status to avoid calendar duplication.
- Must check for existing plan entries before writing (don't clobber user edits).
- Follow all 5 sub-steps: status -> read -> generate -> present -> write.

## Quality checks

- Briefing under 15 lines (excluding MITs)
- Every MIT names a concrete deliverable
- Skipped phases produce no empty sections
- Calendar events show times, not just names
- Pattern names match exactly from workflows/metacognition/context/
- Sign-in feels like a conversation, not a form fill

## Don'ts

- Don't spend more than 10 minutes. If longer, the routine is becoming a procrastination layer.
- Don't suggest process changes during the briefing.
- Don't dump raw data during sign-in.
- Don't force engagement with every section.

<<wb:obsidian/bridge>>
