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
``styles() -> str``. The registry below imports the functions
explicitly so the concatenation order is visible at a glance.

Concatenation order is load-bearing — see comments inline.
"""

from __future__ import annotations
