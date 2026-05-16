"""Dashboard card renderer modules.

Each module here is the client half of a :class:`~work_buddy.dashboard.
cards.DashboardCard`. It exposes ``script() -> str`` returning JS that
registers a renderer into ``window.wbCardRenderers`` keyed by the card
id. The server-side descriptors live in ``work_buddy/dashboard/cards.py``;
the id string links the two halves.

These modules are concatenated into the page after ``core/card_registry``
(which initializes ``window.wbCardRenderers``). See
``architecture/feature-cards``.
"""

from __future__ import annotations
