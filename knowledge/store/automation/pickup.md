---
name: Pickup-time readiness
kind: reference
description: compute_pickup_readiness pure function -- 5-rule precedence ladder deciding whether a task is ready to execute as-is or should develop first when picked up. Reads creation_effort + user_involvement + provenance + staleness + deadline + has_action_items.
entry_points:
- work_buddy.automation.pickup.compute_pickup_readiness
- work_buddy.automation.pickup.PickupReadiness
tags:
- automation
- pickup
- develop-at-pickup
- readiness
aliases:
- pickup readiness
- compute_pickup_readiness
- develop at pickup
- ready to execute
- develop-first
parents:
- automation
- automation
dev_notes: I considered a weighted-score model and rejected -- the user reads the reason field to understand WHY the engage flow chose develop-first; "score 0.42 < 0.5" is opaque. The 5-rule ladder gives concrete reasons ("agent-inferred sparse capture aged 12 days without user touch -- develop first").
---

# automation/pickup

Module: work_buddy/automation/pickup.py.

## Function

compute_pickup_readiness(task, world_state=None, *, now_iso=None) -> PickupReadiness(ready, reason, signals).

Pure function. Returns a frozen dataclass. The engage flow calls this when a task is picked up (focused or working_on_now); when ready=False, the develop-at-pickup flow runs first.

## 5-rule precedence ladder (highest first)

1. action items already exist (world_state.has_action_items) -> ready. Decomposition done; execute against current step.
2. density='developed' or 'dense' -> ready. User pre-developed.
3. sparse + manual + medium/high involvement + has_deadline -> ready. Sparse-but-intentional; trust the user's brevity.
4. sparse + agent-inferred + low involvement + stale -> NOT ready. Old, weakly-attested capture; develop first.
5. otherwise -> develop-first default per ROADMAP section 3.6 (develop-at-pickup default).

## Tunables (module constants)

- SPARSE_STALENESS_DAYS = 7
- HIGH_INVOLVEMENT_PROVENANCES = {'manual'}

Greppable + visible -- the threshold-tuning discussion is a one-file PR.

## world_state forward-compat

Future work may add attraction_passes and relevance_status to the world_state mapping. Today only has_action_items is consumed.
