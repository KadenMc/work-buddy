---
name: Dashboard — Automation surfaces
kind: concept
description: Dashboard surfaces that project the operating-tier resolver and who-can-act decision into the Today tab. The earlier Review Queue, Daily Log, and Engage tabs were retired when the Threads tab became the canonical resolution surface; what remains is the Today tab and the per-task Auto column on the Tasks tab.
summary: Today tab + the Tasks-tab Auto column are the surviving automation surfaces. Review Queue, Daily Log, and Engage tabs were retired when Threads became the canonical resolution surface; the engage helper survives as a private collaborator of ``_build_today_payload``.
tags:
- dashboard
- today
- automation
aliases:
- automation surface
- today tab
- tier-aware dashboard
parents:
- services/dashboard
- services/dashboard
dev_notes: |-
  ## Performance notes

  **Bulk-prefetch is mandatory for Daily Log.** Initial implementation was N+1: one connection per task and another per event. With 175 distinct tasks / 331 events in a 7-day window, that's ~500 connection opens at ~30ms each on Windows WAL-mode SQLite — 21 second timeout. Fix: two ``IN``-clause queries (one for ``task_metadata``, one for ``task_tags``), then resolve tiers in-memory. Post-fix: 0.38s for the same call. Future endpoints touching state_history should follow this pattern.

  **Review Queue legacy filter.** Without the ``risk_profile_json IS NOT NULL OR automation_tier_achievable IS NOT NULL`` filter, every legacy task surfaces because ``parse_risk_profile(None) → SAFE_PROFILE`` puts everyone at tier-3 by default. The unit-test fixtures all seed populated profiles so this didn't show up in CI; only live-fire testing caught it.

  **Daily Log filters auto-created rows.** ``store.create()`` writes a state_history row with ``old_state=NULL, new_state='inbox', reason='created'`` for every new task. The Daily Log is for *actions taken*, not capture timing — filter is ``old_state IS NOT NULL AND reason != 'created'``. Otherwise tier-4 tasks created and acted on within the same window appear twice.

  **Resolver caching strategy:** *don't*. Per-request memoization is fine but cross-request caching adds invalidation complexity. The resolver is pure and cheap — call it on every read. Tolerance / amplifier policy can change live via config reload; cache invalidation against that is more code than it's worth.

  **Frontend mount divergence.** Review Queue shipped a parallel renderer (``loadReviewQueue`` in scripts/tabs/automation.py) rather than reusing ``mountResolutionSurface``. Justification: data shapes differ — pool entries carry ``pool_run_id`` / ``item_id`` / ``group_intent`` etc. that don't apply to task_metadata rows. CSS classes are shared (``wv-blocker-badge``, ``aut-tier-badge``) for visual consistency. Convergence becomes possible once the per-action-item profile aligns the shapes.

  Hard-coded light card backgrounds (the yellow blocked-by-context nudge, the blue Today contracts banner) need hard-coded dark text colors: var(--text-primary) flips to light in dark mode and becomes unreadable. Caught by live preview in the post-build sweep.
---

# Dashboard automation surfaces

v5 Threads is the canonical resolution surface for everything that needs the user's attention; triage flows through the unified source pipeline and surfaces on the Threads tab via group sub-threads. A **Review Queue** tab (tier-3 outputs), a **Daily Log** tab (tier-4 events), and an **Engage** tab (who-can-act + current-context filter) used to project the operating-tier resolver output directly; those are gone (see "What used to be here" below), and what remains is the Today tab and the per-task Auto column on the Tasks tab.

## What's left

### Today tab

``GET /api/automation/today?contexts=<csv>`` -> composes the engage helper output + the clamp-to-now plan from ``work_buddy.task_me.build_now_plan`` + top-2 recommendations from ``task_me.top_recommendations`` + active-contracts banner. Re-runnable on every refresh.

### Tasks tab — Auto column

The existing Tasks tab still surfaces the resolver: per-row tier badge, typed pipeline-blocker badge (when capped), and last-actor pill, backed by enrichment in ``get_tasks_summary``.

## Internal helpers (no HTTP route)

### ``_build_engage_view_payload``

Lives in ``work_buddy.dashboard.service`` and is called only by ``work_buddy.task_me.load_context_for_task_me``, which composes it into Today's payload. Returns every open task with the operating-tier decision, who-can-act block, and user-now satisfaction. Kept as a private collaborator because Today still needs it; the Engage tab that originally consumed it is gone.

## Visual language

The CSS classes that drove the retired tab cards (``aut-tier-badge``, ``aut-actor-badge``, ``wv-blocker-badge``) are gone. The Tasks tab's Auto column uses its own classes. ``.section-subtitle`` survives in ``scripts/tabs/automation.py`` because Today still consumes it.

## Frontend

- ``scripts/tabs/today.py`` owns ``loadToday`` and the today CSS.
- ``scripts/tabs/automation.py`` is now a tiny shared-utility module   (``_autEsc`` + ``.section-subtitle``); the four loaders that   previously lived there (``loadReviewQueue``, ``loadDailyLog``,   ``loadEngage``, ``loadBlockedByContext``) were deleted along with   their tab panels.
- ``scripts/core/page.py``'s ``staticLoaders`` no longer registers   the three retired tabs.
- The ``v4`` collapse-toggle (``legacy-tabs-toggle`` + the   ``hide-legacy-tabs`` body class) was removed too — every   surviving tab is now visible by default.

## What used to be here

- ``GET /api/automation/review-queue`` — removed.
- ``GET /api/automation/daily-log`` — removed.
- ``GET /api/automation/engage`` — removed (the helper survives   for Today).
- ``GET /api/automation/blocked-by-context`` — removed.
- ``scripts/tabs/automation.py``'s ``loadReviewQueue`` / ``loadDailyLog``   / ``loadEngage`` / ``loadBlockedByContext`` — removed.
- ``tests/unit/test_dashboard_automation_surfaces.py`` and   ``test_dashboard_contexts_surfaces.py`` — removed; the engage   helper is still exercised via ``test_dashboard_today.py``.
