---
name: Dashboard
kind: service
description: Web dashboard for system observability — Flask service, dev mode, remote access, development rules
summary: 'Flask dashboard on port 5127, sidecar-managed, published via Tailscale Serve. Read-only mode supported. CRITICAL: no sibling-localhost fetches from the browser; same-origin /api/* only.'
ports:
- 5127
entry_points:
- work_buddy.dashboard
tags:
- dashboard
- flask
- port-5127
- tailscale
- frontend
- settings
- control-graph
aliases:
- flask dashboard
- tailscale serve
- frontend tabs
- read-only mode
parents:
- services
- services
dev_notes: |-
  ## Hard rule for SSE handler authors

  Do NOT call ``loadReview`` / ``loadTasks`` / ``loadSettings`` / ``loadCosts`` / ``refreshCostsData`` / ``staticLoaders[panel]()`` from any ``window.eventBus.on(...)`` handler. Use the panel's surface mutators instead:

  - Review tab: ``window.reviewSurface.{appendCard, removeCard, updateCard, bumpAttractionPasses, setForcedContextStored}``.
  - Tasks / Settings / Costs / Jobs: ``window.<panel>Surface.refresh()`` — internally morphdom-merges via ``window._wbMorphReplace``, preserving user state.

  The regression test ``test_no_wholesale_loader_calls_in_event_handlers`` in ``tests/unit/test_dashboard_event_bus_frontend.py`` enforces this at the JS string level. See ``architecture/event-bus`` for the full per-card mutation contract.

  ## No global panel-refresh timer

  There is no ``setInterval`` driving panel refresh. The dashboard updates from server-pushed events; ``morphdom-umd.min.js`` v2.7.4 (``frontend/vendor/``) is the surgical-update primitive. If a tab needs periodic refresh, the answer is an event in the taxonomy, not a timer.
---

Web dashboard for system observability + control. Served as a sidecar-managed Flask service on port 5127. Accessible remotely via Tailscale Serve.

## Tabs

**Static:** Overview, Threads, Today, Tasks, Jobs, Chats, Contracts, Projects, Costs. Plus a **Settings** panel reached via a gear icon in the header (off the nav bar by design — Settings is a configuration surface, not a peer of the daily-use tabs).

**Dynamic:** Threads, Triage, and Notifications appear via workflow views, the thread system, and the notification log.

## Settings tab

Primary consumer of the **control graph** (see ``architecture/control-graph`` for the aggregator; ``architecture/health`` for the four-layer mental model the graph fuses). The Settings panel has two sub-tabs: **Status** — the control-graph tree (domain → subsystem → component hierarchy with ``effective_state`` badges, preference toggles (Want / No thanks / Undecided, hidden for ``is_core`` components), Configure / Walk me through action buttons for fixable requirements, universal ``?`` help buttons that spawn interactive Claude Code sessions with structured briefs, a per-component ↻ reprobe button, and clickable bulk-state chips that jump to the first problem node of that state) — and **Activity**, a registry-driven set of cards (Obsidian bridge sparkline, sidecar event log, recent-notifications log). See the Card registry section below.

## Modes and endpoints

* **Dev mode:** ``python -m work_buddy.dashboard --dev`` (auto-reloads on file changes). **Not enabled in sidecar config** — use manually for local development only.
* **Frontend layout:** Each tab is HTML + JS in the ``frontend/`` package. JS lives under ``frontend/scripts/`` in three buckets: ``core/`` (event bus, page shell, helpers, workflow polling, notifications, palette, chat sidebar, form bridge, shared pager, card registry), ``tabs/`` (one module per panel, with ``tabs/threads/*`` as a sub-cluster and ``tabs/cards/*`` for registry card renderers), and ``surfaces/`` (workflow-view renderers). Each module exposes ``script() -> str`` and optionally ``styles() -> str``; ``frontend/scripts/__init__.py`` defines the load-bearing concatenation order via the ``SCRIPTS`` and ``STYLES`` registries.
* **Shared pager:** ``frontend/scripts/core/pager.py`` exposes ``window.wbRenderPager(containerId, total, currentPage, pageSize, onPageFnName)``. Tabs that paginate mount a ``<div class="wb-pager" id="..."></div>`` container and call the renderer after their data fetch resolves. The pager hides itself when ``total <= pageSize``. Class names are ``.wb-pager*`` (styled centrally in ``styles.py``); the Threads tab and the Costs sessions table both use it.
* **Adding a tab (5-step pattern):**
    1. Add a ``<button>`` to the tab bar in ``html.py`` ``_html()``.
    2. Add a ``<div class="tab-panel" id="panel-<name>">`` in the panels section.
    3. Create ``frontend/scripts/tabs/<name>.py`` exposing ``script() -> str`` (and optionally ``styles() -> str``).
    4. Add the loader to ``staticLoaders`` in ``frontend/scripts/core/page.py``.
    5. Add the new module's ``script`` (and ``styles`` if applicable) to the ordered registry in ``frontend/scripts/__init__.py``.
   Settings is atypical — its trigger lives in ``header-meta`` rather than the tab bar, but the panel structure is the same.
* **Remote access:** Published privately via ``tailscale serve --bg 5127`` — the ``tailscale`` component (registered in ``COMPONENT_CATALOG``) gates this with click-to-fix requirements; see ``architecture/health/components`` and ``status/tailscale-status-directions``. The browser only hits same-origin ``/api/...`` routes; all local service reads happen server-side.
* **Read-only mode:** ``dashboard.read_only: true`` in ``config.yaml`` gates mutating POST routes (403) and hides mutation controls in the frontend.

## Card registry (feature cards)

The Settings → Activity sub-view is **registry-driven**: its widgets (Obsidian bridge sparkline, sidecar event log, recent-notifications log) are ``DashboardCard``s, not hand-coded render blocks. ``loadActivity()`` calls ``window.wbMountCards('activity', ...)``, which fetches the active card list and renders each registered renderer. A card may carry a *gate* — a boolean expression over component-active state — so a card whose component is opted out simply does not mount (no placeholder). The bridge card is gated on the ``obsidian`` component; opting Obsidian out also stops the backend bridge probe in ``get_system_state()``. See ``architecture/feature-cards`` for the full pattern — gate AST, registry, endpoint, and how to add a card (including from a plugin).

* ``GET /api/dashboard/cards/<mount_point>`` — active card descriptors for a mount point, gates evaluated against current component preferences. Read-only.

## Right-rail surface (chat sidebar)

The dashboard has a persistent right-side surface — ``wb-chat-sidebar`` — that slides in beside the main content, which squishes left via ``html { padding-right }``. Hosts a ``conversation_chat`` renderer in pane mode. See ``services/dashboard/chat-sidebar`` for the full reusable API; first consumer is the Jobs tab's ``💬 Help me create a job`` button (endpoint ``POST /api/user_jobs/help``).

Distinct from the ``conversation_chat`` workflow-view tab — same renderer, different mount point: a workflow-view tab is a full-tab pane reached via the CHAT toast, while the chat sidebar opens directly without a toast and squishes the active tab rather than replacing it.

## Agent ↔ form bridge

The **chat sidebar** (above) is the *conversation* surface; the **form bridge** is the *interaction* surface — schema-driven, typed, and reusable across forms. See ``services/dashboard/form-bridge`` for the full design. Agents call the single MCP capability ``dashboard_interact`` to fill fields, open the form, click submit, and read state; the dashboard validates against the form's registered ``FormSchema`` and routes events through ``window.wbFormBridge`` to per-form handlers.

## Real-time updates

The dashboard updates in real time from server-pushed events delivered over ``GET /api/events`` (Server-Sent Events). Each event mutates only the specific row(s) it concerns; panels are never wholesale-rewritten. ``bus.heartbeat`` published every 10 s as a liveness signal. See ``architecture/event-bus`` for the full design.

## Control-graph endpoints (added with the Settings tab)

* ``GET /api/control/graph[?force=1]`` — serialized graph + cache info.
* ``POST /api/control/preference`` — toggle component preferences.
* ``POST /api/control/fix/<req_id>`` — apply a fix (programmatic / input_required / agent_handoff).
* ``POST /api/control/help/<node_id>`` — spawn an interactive help session.
* ``POST /api/control/reprobe`` — re-run every tool probe, rebuild the graph.
* ``POST /api/reprobe/<component_id>`` — pre-existing; per-component reprobe, reused by Settings' ↻ button.

All mutating control endpoints are gated by ``_reject_read_only()`` and auto-grant the relevant consent (the click IS the consent, same pattern as workflow-launch).

## Form-bridge endpoints

* ``POST /api/dashboard/interact`` — typed entry point for agents driving forms (called by the ``dashboard_interact`` MCP capability and any other process). Body ``{action, form_id, field?, value?, timeout_seconds?}``.
* ``POST /api/dashboard/interact/result/<request_id>`` — frontend's postback for rendezvous-backed actions (``form_submit``, ``form_get_state``). Body ``{ok, error?, errors_by_field?, fields?}``.

Both gated by ``_reject_read_only()``. See ``services/dashboard/form-bridge`` for the protocol.

## User-job endpoints

* ``POST /api/user_jobs`` — create a user-job file from the Add-job form. Same path the chat-walkthrough agent goes through (via the form bridge's ``submitHandler``), so any future change to validation or payload shape benefits both flows.
* ``POST /api/user_jobs/help`` — open a chat-driven walkthrough. Silently creates a conversation, fire-and-forgets a headless Claude session bound to it, returns ``{ok, conversation_id, title}`` for the frontend to feed into ``wbChatSidebar.open``. Auto-grants ``sidecar:agent_spawn`` once-consent inside the spawn helper.

Both gated by ``_reject_read_only()``.

## Triage flow (no separate dashboard endpoints)

Triage runs through the unified source pipeline (``run_source_pipeline`` capability, dispatching to ``EmailTriagePipeline`` / ``ChromeTriagePipeline`` / ``JournalBacklogPipeline`` / inline-capture). Spawned Threads land on the **Threads tab** for the user to approve/reject/defer per child. There is no separate Review-tab surface or Resolution-Surface endpoints — those were retired in the clarify → Threads migration. Per-cluster actions resolve via the standard Threads action-chip dispatch path.

## CRITICAL for all agents modifying dashboard code

* **Never add browser-side fetches to sibling localhost ports** (5123, 5124, 27125, etc.) — these break on mobile and over Tailscale. All cross-service reads must happen server-side.
* **Gate new POST routes with ``_reject_read_only()``** so read-only deployments stay read-only.
* **Same-origin only** for any fetch from the frontend.
* **Silent conversation create for sidebar-bound chats** — call ``conversations.store.create_conversation`` directly, NOT the ``conversation_create`` capability, so ``_notify_conversation_created`` does not double-mount the conversation as both a CHAT toast/workflow-view tab and a sidebar.
* **Do not subscribe to ``dashboard.form.*`` events directly** from per-tab JS. The ``wbFormBridge`` (``core/form_bridge.py``) owns that event family; tab modules register handlers via ``window.wbFormBridge.register(form_id, ...)``.
