---
name: Automation
kind: concept
description: Lazy-resolution layer that decides how far the agent may take a task (operating tier) and how loudly to resurface it. Pure-function resolvers over stored signals; no I/O, no DB writes — surfaces (dashboard, engage view, audit log) call them per-read.
tags:
- automation
- risk
- tier
- resurfacing
- lazy-resolution
aliases:
- automation tier
- operating tier
- risk resolver
- resurfacing level
---

# Automation

The work_buddy/automation/ package owns the lazy-resolution gates that decide how much of a task an agent may execute autonomously, who can act on it now, how loudly to resurface it, and whether it is ready for execution at pickup time. All decisions are computed from stored signals (risk profile, achievable capability, action contexts, provenance, deadline awareness, attraction passes, ...) -- never stored as the authoritative answer, per the lazy-resolution principle (P7 in ROADMAP section 2).

## Children

- automation/risk -- Slice 4: operating-tier and dynamic resurfacing-level resolvers. Composition rule from ROADMAP section 3.4; the typed pipeline-blocker enum from section 3.3.
- automation/contexts -- Slice 5a: resolve_who_can_act over a CONTEXT_REGISTRY of agent / user environment tokens. Pure-function answers "who can act on this now?" against the live tool-status cache. Caps achievable tier at 1 when the agent cannot satisfy.
- automation/pickup -- Slice 7: compute_pickup_readiness 5-rule precedence ladder. Decides whether to execute as-is or develop-first when a task is picked up.

## Future siblings

Slice 8 will plug attraction + relevance signals into compute_resurfacing_level and compute_pickup_readiness.
