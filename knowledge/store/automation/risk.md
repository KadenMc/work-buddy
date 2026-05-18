---
name: Risk model + automation tiers + dynamic resurfacing
kind: concept
description: Operating-tier and dynamic resurfacing-level resolvers. Pure functions reading the four risk dimensions (financial, privacy, accuracy, compute) + three amplifiers (reversibility, regret_potential, inference_uncertainty) against the user's tolerance config. Returns OperatingTierDecision / ResurfacingDecision dataclasses with typed pipeline_blocker per ROADMAP §3.3.
tags:
- automation
- risk
- tier
- amplifier
- resurfacing
- slice-4
- pipeline-blocker
- lazy-resolution
aliases:
- resolve_operating_tier
- compute_resurfacing_level
- resolve_achievable_tier
- RiskProfile
- RiskTolerance
- AmplifierPolicy
- OperatingTierDecision
- ResurfacingDecision
- risk_profile_json
- automation_tier_achievable
- last_actor
parents:
- automation
- automation
dev_notes: |-
  ## Implementation patterns

  **Frozen dataclasses, not ints.** Resolvers return ``OperatingTierDecision`` / ``ResurfacingDecision`` rather than bare ints/strings. The dashboard Today tab and per-row Tasks-tab Auto column both want *why* the resolver capped, not just the tier; recomputing on the frontend would duplicate the resolver. ``decision.operating`` is a one-attribute access for callers that only want the int.

  **Last-actor detection via consent.get_consent_context_info().** ``_detect_last_actor`` (in ``work_buddy/obsidian/tasks/mutations.py``) reads the consent context — inside ``user_initiated()`` → 'user', otherwise → 'agent'. Wired at three mutation sites (create_task / toggle_task / generic update_task state-change branch); deliberately NOT at the store layer because store-level writes also include reconciliation paths (task_sync re-deriving description) where setting last_actor would be wrong.

  **JSON column for risk_profile.** Forward-compat with Slice 7 (per-action-item profiles) + Slice 8 (attraction signals) — both want the shape evolvable without ALTER TABLE churn. Trade-off: can't ``WHERE accuracy='critical'`` in SQL; if Slice 8 needs it we promote selected dimensions to columns then.

  **Resolver is pure, callable per-render.** No memoization across requests. The dashboard /api/tasks endpoint calls ``resolve_operating_tier`` for every non-archived task on every render (~50 calls × <1ms each on real data). Don't cache: tolerance / amplifier policy can change live via config reload.

  **Achievable-tier inference is v0.** ``resolve_achievable_tier`` infers from the risk profile (irreversible+high-regret → 2, critical-accuracy → 3, default → 3) when ``automation_tier_achievable`` isn't cached. Slice 5a will plug context-aware logic via ``contexts`` kwarg (currently accepted but ignored — kept in signature so callers don't churn).

  **Test fixture footgun.** Resolver unit tests all seed populated profiles, so the legacy-NULL-profile-everywhere production case isn't exercised. The Slice-4 Review Queue + Daily Log endpoints (now retired in the v5 cleanup) surfaced bugs only in live-fire testing. Future slices touching this surface should include at least one legacy-NULL-profile fixture row to model the production state.

  **Amplifier policy can be loosened per-gate.** ``config.local.yaml`` can set ``high_inference_uncertainty_requires_consent: false`` for users who don't want the inference gate. The other two gates (irreversible / high-regret) are individually toggleable too; default is all-ON (conservative).
---

# Risk model + automation tiers + dynamic resurfacing

Slice 4. The single home for the pure-function gating that decides how far an agent may take a task and how loudly the system should resurface it.

## Risk profile

A task carries a ``risk_profile_json`` blob with **four dimensions** + **three amplifiers**:

| Field | Ladder / type | Notes |
|---|---|---|
| ``financial_cents`` | int | estimated max spend, cents |
| ``privacy`` | none / internal / public | action-exposure level |
| ``accuracy`` | low_stakes / consequential / critical | blast radius if output is wrong |
| ``compute`` | instant / background / expensive | resource consumption |
| ``reversibility`` | trivial / moderate / irreversible | amplifier |
| ``regret_potential`` | low / medium / high | amplifier (e.g. sending email under user identity) |
| ``inference_uncertainty`` | low / medium / high | amplifier — agent's calibration on user intent |

NULL profile (legacy, pre-Slice-4) → ``parse_risk_profile(None)`` returns ``SAFE_PROFILE`` (every field at its safest level). Resolvers therefore behave conservatively for unclassified tasks.

## User config

``config.local.yaml`` ``risk_tolerance:`` block sets the per-dimension ceiling for autonomous action. ``amplifier_policy:`` block sets which amplifier firings force consent. Defaults from ROADMAP §3.4: financial 50¢, privacy=none, accuracy=low_stakes, compute=background, all three amplifier gates ON.

## Resolvers

```python
from work_buddy.automation.risk import (
    resolve_operating_tier,        # OperatingTierDecision
    compute_resurfacing_level,     # ResurfacingDecision
    resolve_achievable_tier,       # int (0-4)
)

decision = resolve_operating_tier(task_row, config=cfg)
# decision.operating, .achievable, .allowed_under_risk
# decision.pipeline_blocker (typed string per ROADMAP §3.3) when capped
# decision.capped_by, .reasons (audit trail)
```

All three functions are **pure**: no I/O, no DB writes. Surfaces call them per-read; cheap enough that the dashboard's Tasks tab resolves every active task on every render.

## Composition rule (ROADMAP §3.4)

``operating = min(achievable, allowed_under_risk)``

- Each dimension exceeding tolerance caps allowed at 2 (plan-and-execute) — *except* accuracy, which caps at 3 (output review). Critical-accuracy work is what tier-3 review-queue exists for; tier-2 plan-approval is the wrong UX for 'review the summary I wrote'.
- Each amplifier firing (irreversible / high regret / high inference uncertainty) caps allowed at 2 with a typed ``pipeline_blocker`` indicating which gate fired.
- Amplifier blocker priority: ``consent_required`` (reversibility / regret) > ``inference_uncertain`` (the more concrete reason wins the headline; full list lives in ``reasons[]``).
- ``risk_threshold_exceeded`` is the fallback blocker when no amplifier fires but a dimension capped.

## Resurfacing level

``compute_resurfacing_level`` returns one of ``search_only | digest | triage | alert``:

- ``alert`` — relevance_status=invalidated OR deadline within 2 days.
- ``triage`` — deadline within 14 days OR attraction_passes ≥ 3 OR relevance_status=needs_check.
- ``search_only`` — agent-inferred + sparse + low-involvement + no deadline.
- ``digest`` — default for everything else.

Slice 8 will plug ``attraction_passes`` / ``relevance_status`` signals via the ``signals`` kwarg without touching the resolver shape.

## Schema columns (task_metadata)

Added by Slice 4 via the Slice-2 ``_SLICE_N_COLUMNS`` migration descriptor in ``work_buddy/obsidian/tasks/store.py``:

- ``risk_profile_json`` (TEXT, NULL legal — safe-profile fallback)
- ``automation_tier_achievable`` (INTEGER, NULL legal — resolver re-derives lazily)
- ``last_actor`` ('agent' | 'user' | NULL)

## Typed pipeline blockers (ROADMAP §3.3)

The resolver emits one of these on ``OperatingTierDecision.pipeline_blocker`` when it caps below achievable:

- ``consent_required`` — irreversible reversibility OR high regret amplifier fired.
- ``inference_uncertain`` — high inference_uncertainty amplifier fired (different category from 'this is risky').
- ``risk_threshold_exceeded`` — a dimension exceeded tolerance but no amplifier fired.

The full enum (also covering ``agent_context_unmet`` etc. for Slice 5a) lives in ``work_buddy.clarify.resolution`` so the Resolution Surface card can render presentation hints (label, tone, deep_link) consistently across pre-creation verdicts and post-creation tier decisions.

## Inference uncertainty calibration (Q-i v0)

Default ``medium`` for every LLM-classified task. ``low`` reserved for actions the user directly invoked. ``high`` only via Slice 3's refusal path (the agent declined to commit a verdict). Self-report not relied on. Long-term plan to use logprob-based calibration with Slice 8 attraction signals as ground truth.
