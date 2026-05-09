"""Surface script modules — workflow-view renderers and overlays.

Modules here register view renderers (via ``registerViewRenderer``) or
decorate existing tab content. They don't own a panel themselves —
they mount onto containers managed by ``core.workflows`` or onto an
existing tab's render output.

* ``triage`` — registers ``triage_clarify`` and ``triage_review``
  view renderers
* ``resolution`` — Threads-FSM ``resolution_request`` view renderer
"""

from __future__ import annotations
