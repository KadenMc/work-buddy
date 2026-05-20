---
name: Action Catalog (typed lens over capability + workflow registries)
kind: concept
description: 'Filtered view over the existing capability + workflow registries: entries where is_action=True. Four action kinds: Standard, Improvised, Suggestion, Clarification.'
summary: 'Stage 1 deliverable: capability/workflow definitions gain is_action / available_in / intrinsic_amplifiers / parameter_schema_for_action / requires_post_review. Stage 2 wires action inference through these fields. 2026-05-03: added ActionKind.CLARIFICATION (route to AWAITING_ACTION_CLARIFICATION) and started injecting the catalog into the action-inference prompt.'
tags:
- threads
- actions
- catalog
parents:
- threads
- threads
---

## Four action kinds

- **Standard**: registered capability or workflow. The FSM dispatches into the existing capability call infrastructure or the workflow conductor.
- **Improvised**: agent runs an action without a registered template — multi-turn agent loop with tools, deciding each step as it goes. NO workflow definition synthesised. Always requires user approval. Trace can be promoted to a Standard Action via explicit user gesture.
- **Suggestion**: agent has a concrete advisory recommendation for the user (e.g. "consider archiving this"). Surfaced as a card; agent has no execution role. NOT a low-confidence fallback and NOT for asking the user questions.
- **Clarification**: agent genuinely cannot propose any concrete action — too little information in the inciting context to map to anything. The proposal carries a `blocked_on` field stating exactly what's needed. Routes the FSM to AWAITING_ACTION_CLARIFICATION (short-circuits the policy gate; clarification is not executable). Reserved for cases where the inciting context is too sparse to call any registered standard action — `wb/TODO X` is enough to call task_create with task_text=X, so it should NOT route to clarification.

## How standard actions reach the inferring agent

The Action Catalog is injected into the action-inference prompt as a markdown list of `name: description` + `params:` lines (see `work_buddy.threads.bootstrap._maybe_format_action_catalog`). Without this block the agent can't pick a Standard Action by name and falls back to improvised/suggestion plans. Schema detection is by the presence of `kind: standard` in the schema's enum, so both staged ACTION and COMBINED targets get the catalog.

## Capability/Workflow extensions (Stage 1.5)

- ``is_action: bool`` — opt-in for Action Catalog inclusion.
- ``available_in: set[InvocationContext]`` — defaults to {AGENT_CONVERSATION, AGENT_AUTONOMOUS, ACTION_PROPOSAL, USER_INVOCATION} (every context EXCEPT FSM_INTERNAL). Sensitive ops + FSM internals override.
- ``intrinsic_amplifiers: dict[str, str]`` — risk amplifiers intrinsic to the action (e.g. send_email → {reversibility: irreversible, regret_potential: high}).
- ``parameter_schema_for_action: dict`` — JSONSchema the inference module proposes parameters against.
- ``requires_post_review: bool`` — when False (default), the FSM dispatches → done after success. When True, lands in awaiting_review.

## InvocationContext

5 contexts: AGENT_CONVERSATION, AGENT_AUTONOMOUS, FSM_INTERNAL, ACTION_PROPOSAL, USER_INVOCATION. The gateway derives the caller's context server-side from session metadata; callers do NOT pass it (DESIGN.md §10.3 + correction #14). ``wb_search`` filters by available_in before returning candidates.

## Currently flagged is_action=True

- `task_create` (added 2026-05-03) — canonical Standard Action for "create a new task". Anything that recognizes itself as a TODO request (journal `wb/TODO X` lines, chrome capture parents, etc.) should route here rather than improvise.

When you add an `is_action=True` capability, double-check `available_in` includes `InvocationContext.ACTION_PROPOSAL` — without it the entry is filtered out of the agent-facing catalog and the inference layer can't pick it.
