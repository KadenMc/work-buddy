---
name: Autonomy policy (composed, not enum)
kind: concept
description: Per-Thread policy composed from orthogonal axes. Saved compositions are configuration, not types. Sub-threads override DOWN axis-by-axis only. Stage 5 wired the runtime — auto-advance branch resolvers gate every INFERRING_* → AWAITING_*_CONFIRMATION transition.
summary: 'Axes: auto_advance_states, consent_required_kinds, inference_confidence_floor, irreversibility_threshold, regret_potential_threshold, pause_on_risk_amplifier, allowed_action_kinds, allowed_invocation_contexts, inference_floor/ceiling_tier, budget_usd, combined_inference. Composition is Omegaconf-flavored: global defaults < parent overrides < Thread overrides. Stage 5 added the runtime — every (INFERRING_*, TRIG_INFERENCE_DONE) transition routes through a branch resolver in work_buddy/threads/autonomy_branch.py that reads the policy and decides whether to advance or surface a card.'
tags:
- threads
- autonomy
parents:
- threads
- threads
---

## Saved compositions (named bundles)

Common shapes get names. These are CONFIGURATION, not types — adding a new named composition is a YAML edit, not a code change. Threads can deviate from a saved composition without forking it.

- ``end_to_end`` — auto-advance through inference + confirmation + executing; pause only on risk amplifier.
- ``plan_then_review`` — auto-advance through intent + context confirmations; require user consent on action_approved. **This is the default for spawned threads** (configurable via ``threads.default_autonomy_composition`` in config.yaml).
- ``hands_off`` — every event kind requires user input; no auto-advance.

## Sub-thread inheritance

Children inherit axis-by-axis. Override DOWN (more conservative) is allowed; override UP is not. A parent at ``hands_off`` cannot have a child at ``end_to_end``. Enforced at sub-thread spawn time by the ``decompose`` Standard Action.

## Risk-amplifier safety net

When ``pause_on_risk_amplifier=True``, an action's intrinsic amplifiers OR the Thread's risk profile exceeding the policy's thresholds force ``awaiting_confirmation`` regardless of ``auto_advance_states``.

## Auto-advance branch resolvers (Stage 5 runtime)

The FSM table maps each ``(INFERRING_*, TRIG_INFERENCE_DONE)`` cell to a branch label (``intent_review_or_advance``, ``context_review_or_advance``, ``action_review_or_execute``). The engine's default branch resolver delegates to ``work_buddy.threads.autonomy_branch.resolve_by_label``, which reads the thread's effective policy (walking parents) and decides:

- **Auto-advance** (skip the wait state, go to the next inference target or directly to EXECUTING) iff:
  1. The would-be wait state is in ``policy.auto_advance_states``.
  2. ``confidence >= policy.inference_confidence_floor``.
  3. The proposal's event kind is NOT in ``policy.consent_required_kinds``.
  4. (action only) action's irreversibility ≤ ``policy.irreversibility_threshold``.
  5. (action only) action's regret_potential ≤ ``policy.regret_potential_threshold``.
  6. (action only) NOT (``policy.pause_on_risk_amplifier`` AND action.has_risk_amplifier).
  7. (action only) action.kind in ``policy.allowed_action_kinds``.

- **Surface** (enter the AWAITING_*_CONFIRMATION state) otherwise.

The wait state is **never entered** when auto-advance applies — its state-entry handlers (notification publish, etc.) never fire. Every decision (positive or negative) is recorded as an ``auto_advance_decision`` event with the per-axis trace so the user can audit what the agent decided on its own.

## Combined-inference axis (Stage 5)

``combined_inference: bool`` (default False) opts the thread into single-LLM-call inference: one ``InferenceTarget.COMBINED`` call returns intent + context + action together. The worker records three separate ``*_inferred`` events plus a ``combined_inferred_meta`` audit event, and walks the FSM through all three INFERRING states with the autonomy resolver gating each step. Combined is an optimization (one call vs three), not a policy bypass — every transition still respects ``auto_advance_states``, confidence floor, and risk thresholds.

## Notification surface targeting (Stage 5)

Differentiated via ``work_buddy.threads.resolution_surface._surfaces_for(rr)``: legitimate user-pause states (``AWAITING_CONFIRMATION``, clarifications, post-execution review, failed cleanup) fan out across all surfaces (Obsidian + Dashboard + Telegram). The intent/context confirmation states — when reached because confidence was below the floor or the policy is conservative — only land on the dashboard, never on Telegram or Obsidian. Reduces noise without losing visibility.

## Mid-process visibility toggle (Stage 5)

The dashboard threads list hides ``AWAITING_INFERENCE`` / ``INFERRING_*`` / ``EXECUTING`` / ``MONITORING`` / ``CLEANING_UP`` states by default (the user has nothing to act on). A 'Show mid-process' toggle (query param ``include_mid_process=1``) surfaces them with a muted style and a ``display_mode='mid_process'`` render hint, useful for auditing 'what is the agent doing right now?' and for debugging.

## Where this lives in code

- ``work_buddy/threads/autonomy.py`` — saved compositions, override-down validator, ``default_spawn_policy()`` config helper.
- ``work_buddy/threads/autonomy_branch.py`` — the runtime resolvers (intent/context/action).
- ``work_buddy/threads/fsm.py`` — branch labels in TRANSITION_TABLE.
- ``work_buddy/threads/engine.py`` — default resolver delegates by label, writes the audit event after state_transition lands.
- ``work_buddy/threads/inference.py`` — TargetSpec for ``InferenceTarget.COMBINED`` with the merged schema.
- ``work_buddy/threads/inference_worker.py`` — ``_process_combined`` orchestrates the three FSM transitions for a single combined call.
- ``work_buddy/threads/resolution_surface.py`` — ``_surfaces_for`` decides notification fan-out per state.
