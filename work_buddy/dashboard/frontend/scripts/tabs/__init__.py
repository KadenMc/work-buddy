"""Tab script modules — one per panel in ``staticLoaders``.

Each module owns:

* a loader function (``loadOverview``, ``loadJobs``, …) referenced from
  ``core.page``'s ``staticLoaders`` registry
* optionally a ``window.<name>Surface`` handle so the SSE event bus
  (``core.event_bus``) can call surgical mutators (``refresh``,
  ``appendCard``, etc.) without re-rendering the whole panel
* the JS helpers it uniquely needs

``threads/`` is a sub-package because the threads cluster has internal
coupling between card / actions / group rendering — keeping them
together signals that boundary explicitly.
"""

from __future__ import annotations
