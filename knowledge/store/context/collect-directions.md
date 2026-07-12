---
name: Context Collection Directions
kind: directions
description: How to collect context and synthesize an orientation — priority order, flags, contract cross-reference
summary: Collect via context_bundle, read in priority order (git -> tasks -> projects -> obsidian -> chats), synthesize 10-15 line orientation. Cross-reference against active contracts. Suggest one next action.
trigger: user wants to know their current work state or collect context
command: wb-context-collect
workflow: context/collect-and-orient
capabilities:
- context/context_bundle
- context/context_git
- context/context_tasks
- contracts/active_contracts
tags:
- context
- collection
- orientation
- synthesis
- directions
aliases:
- collect context
- orient
- context bundle
- current work state
- what am I working on
parents:
- context
---

Use mcp__work-buddy__wb_run("context_bundle") to collect. Scope options:
- Default: config.yaml windows
- Quick: {"hours": 24}
- Last N days: {"days": 3}
- Exact window: {"since": "18h", "until": "now"} or ISO datetimes — a precise, minute-level window every source honors (wins over hours/days)
- Individual: context_git, context_chat, etc.

## Synthesis instructions

Read each context file in priority order (git -> tasks -> projects -> obsidian -> chats). Extract the signal -- don't dump raw contents.

Present a concise orientation covering:

Active work (from git):
- Which repos had recent commits? What kind of work?
- Any dirty working trees? (uncommitted changes = in-progress work)

Projects (from projects summary):
- What projects are currently active? State?
- Do active projects align with where time is spent?

Current state (from Obsidian):
- Journal entries today? Sign-in data?
- Incomplete tasks? Any tagged as focused?

Recent conversations (from chats):
- Last few sessions about? Any unfinished work?

Flags -- surface when data warrants. Name patterns only if they exist in the user's documented personal knowledge (knowledge_personal, any category they track); otherwise describe the signal plainly:
- No journal entry today -> user hasn't checked in
- Running Notes growing without triage
- Git activity in repos not related to any contract -> potential drift
- Lots of infra commits, no corresponding written output
- Projects with no recent activity -> stale or abandoned?

## Contract cross-reference

Cross-reference against active_contracts:
- Does git activity align with any active contract?
- Is there work happening that doesn't map to any contract?

If no contracts exist: 'No active contracts -- all work is currently unanchored.'

## Suggest one next action

Based on orientation, suggest one concrete next step -- highest-leverage action given current state.

## Output format

Keep to 10-15 lines max. The user doesn't need a report -- they need a mirror.

## Don'ts
- Don't read every file verbatim
- Don't generate a 50-line report
- Don't invent concerns not in the data
- Don't skip the contract cross-reference
- Don't import work_buddy.* modules when a wb_run capability exists
