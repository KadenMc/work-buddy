---
name: Dashboard — Costs tab
kind: concept
description: LLM cost / usage view with two complementary sources (per-call internal log + Claude Code transcripts), row-level backend filters, and the Anthropic rate-limit observation chip. The unified ``llm_costs_query`` capability reads both sources.
tags:
- dashboard
- costs
- llm-cost
- llm-usage
- transcripts
- claude-code-usage
aliases:
- costs tab
- llm costs
- claude code costs
- transcript costs
- cost dashboard
- llm spend
parents:
- services/dashboard
- services/dashboard
dev_notes: |-
  ## Refresh paths and chip handlers

  Two fetch paths, both routing through the shared ``_costsBuildParams({includeModels})`` helper that translates ``costsState`` into URL params:

  * ``loadCosts()`` — full reload (project / range / activity changes). Fetches with ``includeModels=false`` so the response carries the unfiltered ``all_models``, then snapshots it into ``costsState.knownModels``.
  * ``refreshCostsData()`` — data-only refresh used by chip-change handlers. Fetches with ``includeModels=true`` so an active narrowing propagates to every backend aggregate; re-renders without rebuilding the chip rail (``costsRenderAll({skipModelsFilter: true})``).

  **No more auto-tick.** The 30s ``setInterval`` that previously re-ran ``refreshCostsData`` is gone. The Costs tab now refreshes from the event bus: ``llm.call_logged`` events route through the smart-refresh policy (see ``architecture/event-bus``) and call ``loadCosts()`` when the panel is active and no input is focused.

  Chip handlers (``costsModelToggle``, ``costsModelSolo``, ``costsModelFamilyToggle``, ``costsModelFamilySolo``, ``costsModelsReset``) all flow through ``_costsAfterModelChipChange``: (a) re-render chips locally for immediate visual feedback, then (b) call ``refreshCostsData`` for backend re-aggregation.

  ## Toolbar Refresh button + empty-state CTA share ``costsRefresh``

  ``costsRefresh(btn)`` POSTs ``/api/costs/rescan`` first, then awaits ``loadCosts(true)``. Both the toolbar Refresh button and the empty-state "Rescan Claude Code" button call it; the helper captures and restores ``btn.textContent`` so each surface keeps its own label while sharing one code path. Read-only mode 403s the rescan endpoint — the helper detects 403 and falls through to a plain reload rather than surfacing an error.

  The rescan step is load-bearing. Wiring the button to ``loadCosts(true)`` directly leaves the Claude-Code cache stale because ``loadCosts`` only re-fetches ``/api/costs`` — it never re-ingests transcript JSONLs. Without the rescan, clicking Refresh against a stale cache renders identically. Don't simplify it away.

  ## Passive refresh via sidecar job

  ``sidecar_jobs/claude-code-usage-scan.md`` runs ``claude_code_usage_scan`` at midnight + noon local with 300s jitter. Without this, the cache only advances when a user manually clicks Refresh — and the chart can drift stale for days unnoticed. The scanner is incremental by file mtime, so a missed tick (laptop asleep) is self-healing; the next run picks up everything written since the last successful ingest.

  ## knownModels snapshot — don't remove this

  The chip rail reads its model list from ``costsState.knownModels``, NOT from ``data.all_models``. This matters: a narrowing refetch returns a smaller ``all_models`` (filtered to the narrowed set), and if chip rendering used that, the rail would shrink to only-selected chips and you couldn't get back to the unselected ones. The snapshot is taken on the most recent unfiltered fetch (i.e. inside ``loadCosts``) and only updated then. Future refactors that try to "simplify" by reading ``all_models`` directly will break the chip rail — don't.

  ## models trichotomy in code

  Aggregators preserve the missing/empty/populated trichotomy via ``None`` (no filter) vs ``[]`` (match nothing) vs populated list. Service code translates the URL param accordingly: ``request.args.get('models')`` is ``None`` if the key is missing and ``''`` if present-but-empty, so the route uses ``'models' in request.args`` to distinguish (do not collapse via ``or None``). The SQL aggregator emits ``WHERE 0`` for the empty case rather than dropping the WHERE clause entirely.

  A prior version collapsed ``[] -> None``, silently falling back to all-time data when the user de-selected every chip. That was the bug commit ``dece62f`` fixed; the test ``test_aggregator_models_empty_list_matches_nothing`` is the regression guard.

  ## Cost-log timestamp format

  ``work_buddy.llm.cost.log_call`` writes ``datetime.now(timezone.utc).isoformat()`` (UTC, explicit offset). Pre-2026-04-26 rows lack timezone info; the frontend's ``_costsParseTs`` defensively appends ``Z`` to TZ-naive strings so ``new Date(...)`` interprets them as UTC, not local time. Without that the active-dot logic mis-fires on non-UTC machines.

  ## Rate-limit observation pipeline

  ``work_buddy.llm.runner`` wraps Anthropic calls with ``client.messages.with_raw_response.create()`` — with an ``AttributeError`` fallback for SDK versions that don't expose the raw-response wrapper. Captured ``anthropic-ratelimit-*`` headers go to ``work_buddy.llm.rate_limits.record_observation(model, headers)``, which persists the latest-per-model observation under ``<data_root>/runtime/rate_limits.json``. The dashboard reads the file at request time on ``GET /api/costs/rate-limits``; the chip computes most-restrictive headroom across recently-observed models. There's no aggregation or admin gate — just per-model latest-observation persistence; if you add admin features here, expect to revisit the storage shape.

  ## Refresh-bug guardrail — the bug is fixed; do not bring it back

  The dashboard's tab-load path was historically prone to a chronic refresh bug: a 30s ``setInterval`` re-ran the active tab's loader, which rewrote ``panel.innerHTML`` and destroyed in-flight UI state (focused textareas, scroll, drawer contents). Two prior fix attempts (snapshot/restore in commits 5a5c0a2 / 4233d68; the ``dataRefreshers`` table in commit cd73918) were partial. The whole timer + dataRefreshers table is now deleted; the dashboard updates from the event bus (``architecture/event-bus``).

  **Do not bring the timer back.** If a tab needs periodic refresh, ask first: is there a server-side signal that should publish an event? Almost always yes — add the event to the taxonomy and route through ``events.publish_auto``. The regression test ``test_legacy_30s_timer_is_gone`` enforces the guardrail at the JS string level: it scans ``scripts/core/page.py``'s ``script()`` output for ``setInterval(`` (only ``updateClock``'s is allowed), ``dataRefreshers``, and ``startAutoRefresh`` and fails if any return.

  ## turns rollup horizon

  `prune_claude_code_usage_db` collapses `turns` rows older than `days_to_keep_full` (default 90) into per-(session, day, model) aggregates in the `turns_daily` table, then deletes the originals and VACUUMs. The aggregator's read queries (`by_day`, `by_model`, `by_project`, the sessions-table totals) already group beyond per-turn granularity, so the rollup is lossless for everything the dashboard renders. Only loss is per-turn drilldown of pre-horizon history; the UI never exposed that, so the surface contract is unchanged.

  Two implementation knots worth knowing:

  - The rollup INSERT uses `ON CONFLICT(session_id, day, model) DO UPDATE … = … + excluded.…` so re-running with overlapping aggregates accumulates correctly (idempotent additive merge, not last-write-wins). A naive `INSERT OR REPLACE` would silently double-count if the same horizon-pass ran twice on the same data.
  - VACUUM cannot run inside a transaction. The pruner explicitly commits before VACUUMing; SQLite's default autocommit-after-commit handles that. If a future change wraps the whole pruner in a `with conn:` block, VACUUM will throw `cannot VACUUM from within a transaction` — keep the explicit commit.
---

LLM cost / usage view. Two complementary sources, picked via the toolbar:

* **Internal log** — every ``LLMRunner`` API call, written to ``<data_root>/agents/<session>/llm_costs.jsonl`` by ``work_buddy.llm.cost.log_call``. Captures cloud + local backends.
* **Claude Code** — Claude Code's per-session JSONLs in ``~/.claude/projects/``, ingested into a SQLite cache at ``<data_root>/cache/claude_code_usage.db``. Captures every Claude Code session on the machine, regardless of whether it touched work-buddy.

The two sources are **complementary, not overlapping** — work-buddy's runner calls go ONLY into the internal log; Claude Code sessions go ONLY into transcripts. Summing them under ``source=all`` is honest with no de-dup needed.

## Surfaces

* ``GET /api/costs?source={internal|claude_code|all}&...`` — read the dashboard read model. See *Query params* below. Legacy ``source=transcripts`` still routes to ``claude_code``.
* ``GET /api/costs/projects`` — list of canonical project names for the toolbar dropdown.
* ``GET /api/costs/rate-limits`` — most-recent per-model rate-limit observations (RPM / ITPM / OTPM headroom) for the toolbar chip.
* ``POST /api/costs/rescan`` — refresh the Claude Code cache (gated by read-only mode).
* Capability ``llm_costs_query`` — the **primary programmatic surface**. One call covers most cost questions: time windows (named or ISO range), grouping (project / model / session / day / tool), source filter, comparison-to-previous-window. See its parameter schema for details.
* Capability ``claude_code_usage_scan`` — trigger an incremental rescan (mutates state).
* Capability ``escalation_recent`` — per-tier LLM escalation observability records (logged separately at ``<data_root>/logs/escalations.log``; pruned via ``logs/escalations`` registered in ``paths.PRUNERS``).

## /api/costs query params

All filters apply at row level in *both* aggregators so totals / by_day / by_model / by_task / sessions / etc. stay in sync with the toolbar — no client-side post-filtering of cards or charts.

* ``project=<name>`` — substring match against the canonical project name (the chats-tab resolver collapses worktrees to their parent project, e.g. ``electricrag-fg-clep`` → ``electricrag``).
* ``execution_mode={cloud|local}`` — only meaningful for the internal source; ``claude_code`` rows are always cloud.
* ``start_date=YYYY-MM-DD`` / ``end_date=YYYY-MM-DD`` — inclusive bounds.
* ``models=<csv>`` — comma-separated model allow-list. **Trichotomy:**
  * missing param → no filter (all rows)
  * ``models=`` (present, empty value) → match nothing (zero rows)
  * ``models=a,b`` → narrow to those models
  
  The empty-vs-missing distinction is part of the contract: it's how the chip rail expresses "user de-selected every model" without silently falling back to all-time. Capability and operational callers must emit `models=` (with no value) when the intent is "return zero rows," not omit the param.

## Reading the numbers

The internal source records cost only for cloud calls; local-LLM calls log ``estimated_cost_usd: 0.0`` by design. Both sources price cloud calls against the canonical Anthropic table at ``work_buddy.llm.claude_code_usage.pricing`` (input + output + cache_read at 90% off + cache_creation at +25% premium). Cards split the Calls count into ``cloud N · local M`` so the cost number is unambiguous. Costs above $100 drop the cents and use a thousands separator.

## Frontend (Costs tab)

Toolbar widgets, in order:

* **Project select** — populated from ``/api/costs/projects``. Reuses the chats-tab canonical project resolver so worktrees collapse to their parent.
* **Activity pills** — only visible when ``project=work-buddy``. Switches between ``all / claude_code / programmatic / api / local``; ``api`` and ``local`` thread an ``execution_mode`` filter to the backend.
* **Date range pill** — translates to ``start_date``.
* **Model filter chips** — grouped by family (currently a single ``Claude`` family). Click toggles a chip; alt/shift-click solos to that one. Family pill toggles the whole group. ``Reset`` link appears when narrowed. De-selecting every chip narrows to zero rows (matches the trichotomy above).
* **Rate-limit chip** — shows the most-restrictive headroom across recently-observed Anthropic models; click to expand a per-model popover. Backed by ``/api/costs/rate-limits``. The headers describe **burn rate** (token-bucket replenishment), not session-total quota — they show how close you are to being throttled right now, not how much you've spent overall.

The sessions table shows an **active dot** next to sessions whose last activity is within ``ACTIVE_WINDOW_MINUTES``. The ``.wb-active-dot`` CSS class is reusable — the Chats tab uses the same one.
