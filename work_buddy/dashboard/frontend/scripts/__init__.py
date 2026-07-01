"""Dashboard frontend script modules.

Each script module is a Python file that returns a JS string (and
optionally a CSS string) describing one slice of the single-page app.
The page is assembled by ``frontend/__init__.py`` from the ordered
``SCRIPTS`` and ``STYLES`` registries defined here.

Three buckets:

* ``core/`` — cross-cutting infrastructure: event bus, helpers, page
  shell (tab switching, URL hash, init), workflow-view polling,
  notifications, command palette.
* ``tabs/`` — one module per panel listed in ``staticLoaders``. Each
  owns its loader and (where applicable) its ``window.<name>Surface``.
  ``tabs/threads/`` is a sub-package because the threads cluster has
  internal coupling between card / actions / group rendering.
* ``surfaces/`` — workflow-view renderers and decorator overlays that
  mount onto existing tabs (triage clarify/review, resolution).

Module API: each module exposes ``script() -> str`` and optionally
``styles() -> str``. The triage module exposes two scripts
(``clarify_script``, ``review_script``) because both renderers share
the same source file.

The ``SCRIPTS`` and ``STYLES`` lists below are the single source of
truth for concatenation order. Order is load-bearing — see comments
inline.
"""

from __future__ import annotations

from .core import (
    card_registry,
    chat_sidebar,
    delegation,
    event_bus,
    filters,
    form_bridge,
    helpers,
    notifications,
    page,
    pager,
    palette,
    workflows,
)
from .surfaces import resolution, triage
from .tabs import (
    automation,
    chats,
    contracts,
    conversations,
    costs,
    embeddings,
    fleet,
    inference,
    jobs,
    memory,
    projects,
    settings,
    tasks,
    today,
)
from .tabs.cards import event_log as card_event_log
from .tabs.cards import notification_log as card_notification_log
from .tabs.cards import obsidian_bridge as card_obsidian_bridge
from .tabs.threads import actions as threads_actions
from .tabs.threads import card as threads_card
from .tabs.threads import group as threads_group
from .tabs.threads import main as threads


# Concatenation order is load-bearing.
#
# * ``event_bus`` first — defines ``window.eventBus``; other modules' top-
#   level code may call ``window.eventBus.on(...)`` at script-load time,
#   so the bus API must already exist when they execute.
# * ``helpers`` second — defines ``fetchJSON``, ``statusBadge``, the
#   health-tree primitives. Every tab loader and surface depends on these.
# * ``workflows`` before view-renderer surfaces (``triage``, ``resolution``)
#   — it owns ``registerViewRenderer`` which those surfaces call at
#   script-load to register themselves.
# * ``triage.review_script`` before ``resolution.script`` — the resolution
#   renderer composes onto ``renderTriageReview`` from the triage script.
# * ``threads_card``, ``threads_actions``, ``threads_group`` after
#   ``threads.script`` — the latter publishes ``window.threadsSurface``
#   onto which the cluster modules attach.
# * ``card_registry`` after ``helpers`` (the mounter uses ``fetchJSON`` /
#   ``_wbMorphReplace``) and before the ``card_*`` renderer modules,
#   which assign into the ``window.wbCardRenderers`` map it initializes.
# * ``page`` LAST. ``core/page.py`` runs init at script-load time:
#   ``_loadJobRegistry()`` synchronously and ``_initFromHash`` queued
#   for ``DOMContentLoaded``. Both touch ``let``/``const`` declarations
#   that live in tab modules (``_jobRegistryPromise`` in tabs/jobs.py,
#   ``costsState`` in tabs/costs.py, ``chatsState`` in tabs/chats.py).
#   Those vars stay in the Temporal Dead Zone until their declaring
#   module evaluates, so page.py MUST run after every tab module —
#   touching a TDZ ``let`` raises ReferenceError and halts the whole
#   ``<script>`` block.
SCRIPTS = [
    event_bus.script,
    helpers.script,
    # delegation: event-delegation dispatcher (window.wbAction / wbActAttrs).
    # After helpers (uses escapeHtml), before page (init at load). Declares
    # no module-scope let/const — page-LAST safe.
    delegation.script,
    card_registry.script,
    card_obsidian_bridge.script,
    card_event_log.script,
    card_notification_log.script,
    pager.script,
    # filters: core/ chip-filter widget (window.wbRenderFilters). Uses
    # escapeHtml (helpers, above) and declares only window.* assignments +
    # hoisted function declarations, so it is safe ahead of page.script.
    filters.script,
    workflows.script,
    notifications.script,
    chat_sidebar.script,
    form_bridge.script,
    triage.clarify_script,
    triage.review_script,
    resolution.script,
    automation.script,
    today.script,
    settings.script,
    conversations.script,
    jobs.script,
    tasks.script,
    chats.script,
    contracts.script,
    projects.script,
    memory.script,
    threads.script,
    threads_card.script,
    threads_actions.script,
    threads_group.script,
    palette.script,
    costs.script,
    embeddings.script,
    inference.script,
    fleet.script,
    # page LAST — see ordering note above.
    page.script,
]

STYLES = [
    filters.styles,
    embeddings.styles,
    inference.styles,
    fleet.styles,
    resolution.styles,
    threads.styles,
    threads_card.styles,
    threads_actions.styles,
    threads_group.styles,
    automation.styles,
    today.styles,
    chat_sidebar.styles,
    memory.styles,
]
