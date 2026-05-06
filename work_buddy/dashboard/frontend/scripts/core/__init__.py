"""Core script modules — cross-cutting infrastructure.

These run on every page load and are depended on by every tab module:

* ``event_bus`` — SSE client + smart-refresh dispatcher
* ``helpers`` — shared JS utilities (``fetchJSON``, status badges,
  health-tree rendering, time formatters)
* ``page`` — page shell: tab switching, URL-hash routing, clock,
  ``staticLoaders`` registry, init, visibilitychange listener
* ``workflows`` — dynamic workflow-view tab polling and renderer
  dispatch system
* ``notifications`` — toast + browser notification UI
* ``palette`` — command palette overlay
"""

from __future__ import annotations
