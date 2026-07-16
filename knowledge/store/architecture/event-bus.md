---
name: Dashboard Event Bus
kind: concept
description: In-process pub/sub + SSE stream + localhost cross-process ingress that powers real-time dashboard updates without a global panel-refresh timer.
tags:
- dashboard
- sse
- event-bus
- real-time
- pub-sub
- cross-process-events
- smart-refresh
aliases:
- event bus
- dashboard sse
- server-sent events
- real-time updates
- /api/events
- publish_auto
- internal bus
parents:
- architecture
- architecture
dev_notes: |-
  ## SSE handlers must mutate single rows

  Do NOT call ``loadReview`` / ``loadTasks`` / ``loadSettings`` / ``loadCosts`` / ``refreshCostsData`` / ``staticLoaders[panel]()`` from any ``window.eventBus.on(...)`` handler. Use the panel's surface mutators instead. The regression test ``test_no_wholesale_loader_calls_in_event_handlers`` in ``tests/unit/test_dashboard_event_bus_frontend.py`` enforces this at the JS string level: the dispatcher source MUST NOT contain ``loadReview(``, ``loadTasks(``, ``loadSettings(``, ``loadCosts(``, ``refreshCostsData(``, ``staticLoaders[``, ``_smartRefresh``, ``_panelHasUserContent``, or ``pendingPanels``.

  For a new event:
  1. If the affected panel is read-mostly with minimal inline edit state, route to ``window.<panel>Surface.refresh()`` which calls the renderer through ``window._wbMorphReplace`` (Tasks / Settings / Costs / Jobs pattern).
  2. If the panel has rich per-row state (drag-drop, multi-stage forms, decorator layers), expose a first-class handle with explicit per-row mutators (Review pattern, see below).

  ## Per-card mutation contract — two flavours

  **(a) First-class handle with explicit mutators — used by Review tab.**

  ``renderTriageReview`` returns:

  ```js
  {
      appendCard(group),                            // pool.entry_added
      removeCard(run_id, item_id),                  // pool.entry_state_changed (terminal)
      updateCard(run_id, item_id, freshGroup),      // pool.entry_state_changed (non-terminal)
      bumpAttractionPasses(run_id, item_id, count), // pool.attraction_passes_bumped
      setForcedContextStored(run_id, item_id),      // pool.forced_context_stored
      isMounted(),
  }
  ```

  Stashed on ``window.reviewSurface`` for the dispatcher. Each mutator is responsible for: animation cancellation (``Element.getAnimations({subtree:true}).forEach(a => a.cancel())`` before applying ``wv-leaving``), focus capture and restore to nearest sibling, drag-state nulling (``dragItem`` and ``dragSourceGroup``) when removing the card a drag points into, state-dict pruning (``state.groups``, ``state.decisions``, ``state.itemOverrides``, ``state.taskAssignments``, ``state.namespaceTags``, ``state.newTaskTexts``, ``state.overrideReasons``), aria-live announcement.

  **(b) Morphdom-light refresh — used by Tasks, Settings, Costs.**

  The panel's renderer runs into a detached node, then ``window._wbMorphReplace(container, html)`` morphdom-merges into the live container. ``window.<panel>Surface = { refresh(), isMounted() }``. The dispatcher debounces 250 ms to coalesce bursts (e.g. ``probe_all`` emits 8+ ``component.health_changed`` events at once).

  ## Jobs surface — two-event temporal pattern

  The Jobs tab subscribes to two events that fire in close succession during user job creation:

  1. ``user_job.created`` is published in-process by ``api_user_job_create`` immediately after ``create_user_job_file`` succeeds. It's the same-request signal for the form's pending banner — the dashboard knows a file was written.
  2. ``cron.hot_reload`` lands ~50 ms later, after the sidecar's ``JobsWatcher`` (filesystem observer) sees the file appear and triggers ``Scheduler._hot_reload``. The scheduler only publishes the event when fingerprints change — idle reloads (no diff) don't ping the bus.

  Both route through ``_refreshSoon('jobsSurface')`` (250 ms debounce) so the burst coalesces to one ``loadJobs()`` call. The pending banner clears in the same render pass when the new row appears in ``/api/state``.

  If you add another publisher of ``cron.hot_reload``, preserve the "only when fingerprints change" filter — firing on every reload would inflate bus traffic with no UI benefit.

  ## ``appendCard`` insertion anchor

  ``appendCard`` inserts BEFORE the ``.wv-new-group-zone`` and ``.wv-section`` anchors, NOT at the end of the container — otherwise new cards land below the "Submit All" footer where the user can't see them. Same pattern as ``createNewGroupWithItem`` in ``surfaces/triage.py``.

  ## ``appendCard`` and ``removeCard`` coordinate via ``_pendingRemovals``

  In-process events have ~0 ms delivery latency; cross-process events arrive via a best-effort POST to ``/internal/bus`` — low latency, but a separate request from the in-process publish. A ``pool.entry_state_changed`` for a just-submitted entry can therefore still arrive before the corresponding ``pool.entry_added``. ``removeCard`` records the key in a closure-local ``_pendingRemovals`` Set when the target card is absent; ``appendCard`` consults that set first and discards a late add for an already-resolved entry. Tasks / Settings / Costs use morphdom-merge refresh which is naturally idempotent against this race.

  ## Cross-process publishes go straight to the dashboard

  ``publish_cross_process`` POSTs ``{event_type, payload}`` to the dashboard's loopback-only ``POST /internal/bus`` endpoint, which re-publishes on the in-process bus. No durable store sits in the path: an event that arrives while no browser is subscribed is dropped, matching the bus's best-effort, no-replay contract. A dashboard that is down drops the event silently — the publisher never blocks and never auto-spawns the dashboard. The endpoint is gated to ``127.0.0.1`` / ``::1`` because the dashboard has no auth and can be bound to ``0.0.0.0`` / published over Tailscale.

  ## Layering: clarify/, tasks/, health/, llm/ all import work_buddy.dashboard.events

  The bus is structurally part of the dashboard layer because the dashboard is the consumer, but publishers across multiple layers (clarify/, obsidian/tasks/, health/, llm/, tools.py) import ``events.publish_auto``. This is a soft layer break, accepted because the bus is a single function call, not a UI dependency. If the layering ever needs to be cleaner, move ``events.py`` to ``work_buddy/events.py``.

  ## ``EventBus.subscribe()`` registers on first iteration

  The subscriber is registered when the inner generator first iterates. The SSE wrapper yields ``: connected`` BEFORE entering the for-loop, so the very first ``next(gen)`` does NOT yet register. Tests must call ``next`` twice to observe ``subscriber_count() == 1``. The race window in production is microseconds; immaterial for single-user.

  ## Animations + accessibility

  Shared CSS keyframes in ``styles.py``: ``wb-row-fade-in`` (250 ms, used via ``.wv-incoming``), ``wb-row-fade-out`` (200 ms collapse, used via ``.wv-leaving``), ``wb-pulse`` (300 ms accent flash, used via ``.wv-pulse``). The ``.visually-hidden`` utility hides ARIA live regions visually while exposing them to assistive tech. The Review surface includes a hidden ``role="status" aria-live="polite"`` region; ``appendCard`` and ``removeCard`` write to it for WCAG 4.1.3 AA compliance.
---

## Why

The dashboard updates in real time from server-pushed events delivered over Server-Sent Events. Each event mutates only the specific DOM nodes it concerns; a panel is never wholesale-rewritten in response to an event. This preserves user state — focused inputs, scroll position, drilled-in `<details>`, chip rails — across every update.

## Relationship to the durable Events backbone

This bus is the **lossy real-time UI** layer — drop-oldest, no durability, no dedup — and is **not** the durable delivery spine. The first-class `events` backbone (`work_buddy.events`) owns at-least-once, deduped, offset-tracked delivery of *event-shaped facts* to reacting consumers; it uses this bus only as its immediate, best-effort **UI projection** (its `publish()` fans out here via `publish_auto`). Rule of thumb: reliable *reactions* read the durable `events` log; live *UI* reads this bus.

## Surfaces

* **Python**
  * ``work_buddy.dashboard.events.EventBus`` — thread-safe in-process pub/sub with per-subscriber bounded ``deque`` + ``threading.Condition``.
  * ``events.publish(event_type, payload)`` — in-process publish.
  * ``events.publish_cross_process(event_type, payload)`` — POSTs ``{event_type, payload}`` to the dashboard's loopback ``/internal/bus`` endpoint.
  * ``events.publish_auto(event_type, payload)`` — routes by process flag (``mark_dashboard_process()`` set in ``service.main()``).
  * ``events.start_heartbeat(interval, bus)`` — publishes ``bus.heartbeat`` every ``interval`` seconds (default 10 s).
  * ``work_buddy.clarify.capabilities.triage_review_pool.compose_entry_presentation_group(entry)`` — single-entry presentation composer used by ``ClarifyPool.submit`` / ``submit_raw`` for fat-add events.

* **HTTP**
  * ``GET /api/events`` — SSE stream. No read-only gate. ``Cache-Control: no-cache``, ``X-Accel-Buffering: no``. 15 s idle keepalive comment to defeat intermediary idle-close.
  * ``POST /internal/bus`` — loopback-only ingress for cross-process publishers. Validates ``event_type`` and re-publishes ``{event_type, payload}`` on the in-process bus. Gated to ``127.0.0.1`` / ``::1``; exempt from the read-only gate (UI-refresh events must flow even in display-only mode).

* **Browser**
  * ``window.eventBus.{on, off, isConnected, lastHeartbeat}`` — per-event-type dispatcher.
  * ``window.<panel>Surface`` — per-panel handle (Review / Tasks / Settings / Costs / Jobs / Projects).
  * Connection-status dot at ``#event-bus-status`` in the dashboard header.
  * The React dashboard owns one EventSource through its event provider. Consumers subscribe through that provider; individual widgets do not open their own SSE connections.

## Vendored dependency

``frontend/vendor/morphdom-umd.min.js`` v2.7.4 (12 KB, MIT) — the surgical-update primitive used by Phoenix LiveView and Hotwire/Turbo.

## Event taxonomy

| Event | Payload | Publisher |
|---|---|---|
| ``bus.heartbeat`` | ``{interval}`` | ``events.start_heartbeat`` (every 10 s) |
| ``pool.entry_added`` | ``{run_id, item_id, source, adapter, raw?, group}`` (fat) | ``ClarifyPool.submit`` / ``submit_raw`` |
| ``pool.entry_state_changed`` | ``{run_id, item_id, state, reason?, outcome?}`` | ``ClarifyPool.mark_state`` / ``mark_reviewed`` (and the wrappers) |
| ``pool.attraction_passes_bumped`` | ``{run_id, item_id, count}`` | ``ClarifyPool.increment_attraction_pass`` |
| ``pool.forced_context_stored`` | ``{run_id, item_id}`` | ``ClarifyPool.store_forced_context`` |
| ``task.created`` | ``{task_id, state, urgency, contract}`` | ``tasks.mutations.create_task`` |
| ``task.state_changed`` | ``{task_id, state, reason}`` | ``update_task`` (when state is set) + ``toggle_task`` |
| ``task.description_changed`` | ``{task_id, description}`` | ``update_task_description`` |
| ``project.created`` | ``{project_id, slug, status, author}`` | ``projects.store.upsert_project`` (new row) |
| ``project.updated`` | ``{project_id, slug, author}`` | ``projects.store.upsert_project`` (existing row), ``update_project`` |
| ``project.deleted`` | ``{project_id, slug, author}`` | ``projects.store.delete_project`` (soft-delete) |
| ``project.folders_changed`` | ``{project_id, action, path, author}`` (action: ``add`` \| ``remove`` \| ``archive`` \| ``unarchive``) | ``projects.store.add_folder`` / ``remove_folder`` / ``set_folder_archived`` |
| ``project.aliases_changed`` | ``{project_id, action, alias, author}`` (action: ``add`` \| ``remove``) | ``projects.store.add_alias`` / ``remove_alias`` |
| ``project.description_confirmed`` | ``{project_id, revision_id}`` | ``projects.store.confirm_description`` |
| ``component.health_changed`` | ``{component_id, available, reason}`` | ``tools.probe_all`` / ``reprobe_one`` (transition-only) |
| ``component.preference_changed`` | ``{component_id, wanted, reason}`` | ``health.preferences.apply_preference_updates`` |
| ``llm.call_logged`` | ``{model, task_id, input_tokens, output_tokens, estimated_cost_usd, execution_mode, cached}`` | ``llm.cost.log_call`` |
| ``cron.hot_reload`` | ``{old_count, new_count}`` | ``Scheduler._hot_reload`` (when fingerprints change) |
| ``user_job.created`` | ``{name, file_path}`` | ``api_user_job_create`` (after successful write) |
| ``inference.call_logged`` | ``{call_id, description, kind, model, execution_mode, status}`` (thin ping) | ``llm.provenance.record_inference_call`` — Settings › Inference refetches the cached ``/api/inference-activity`` |
| ``fleet.changed`` | ``{reason}`` | ``dashboard.api.start_fleet_poller`` (25 s) — the Settings › Inference **fleet** section refetches the cached ``/api/fleet`` and morphs the cards. Published only when a machine's reachability or loaded-model set changes (external LM Studio loads/unloads have no other internal event). |
| ``settings.changed`` | ``{setting_id, scope_id, revision, effective_state}`` | Settings broker after a successful server/profile mutation or reset. React Settings and view contexts reconcile the affected definition/value. |

## Replay semantics

The in-process bus does not replay events from before a subscriber registered. Cross-process events are delivered by a live POST to ``/internal/bus`` with no durable buffer, so an event fired while the dashboard is down — or before the browser's ``EventSource`` connects — is simply not seen. The browser sees events from the in-process bus only AFTER its ``EventSource`` connects. The browser's ``visibilitychange`` listener refreshes the active tab when it returns to foreground after a backgrounded period.

## Refresh patterns per surface

Most surfaces use ``window._wbMorphReplace`` to merge fresh server-rendered HTML into the live container surgically — user state (focused inputs, scroll, drilled-in `<details>`) is preserved natively by morphdom. The ``projectsSurface`` currently uses a full refetch + re-render of the project list sidebar (the detail pane is untouched, so per-card inline edits are preserved). Upgrading projects to morphdom-merge is a planned follow-up.

The React dashboard does not mutate root-dashboard DOM nodes. Its singleton event provider distributes typed events to React consumers, which reconcile only the affected domain state. The transport remains lossy: durable effects and retries never depend on an SSE message being observed.

## Operational notes

* SSE survives Werkzeug ``--dev`` and Tailscale Serve.
* Multi-worker deployment is out of scope (each worker has its own bus; an external broker would be required).
* Slow subscribers drop oldest events with an exposed counter rather than blocking publishers (per-subscriber ``deque`` capped at 1000).
