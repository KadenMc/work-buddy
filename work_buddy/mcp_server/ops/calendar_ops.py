"""Calendar-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field) under
``knowledge/store/calendar/``. The declarations are gated by the
``google_calendar`` tool probe (``requires: [google_calendar]``), so they're
filtered out of the registry when Obsidian / the plugin isn't reachable.

This module registers the calendar capabilities — reads plus the heavy
per-change-consent writes. Write consent lives in the capability layer
(``work_buddy.calendar.capabilities``), not here.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    """Calendar capabilities (reads + writes) exposed by the subsystem.

    All callables flow through
    ``work_buddy.calendar.provider.get_calendar_provider``, which returns the
    configured adapter (the Obsidian bridge today). The ``google_calendar``
    tool probe gates them; the write callables additionally carry heavy
    per-change consent in the capability layer.
    """
    from work_buddy.calendar.capabilities import (
        calendar_coverage,
        calendar_health,
        create_calendar_event,
        delete_calendar_event,
        get_calendar_event,
        list_calendar_events,
        update_calendar_event,
    )

    register_op("op.wb.calendar_health", calendar_health)
    register_op("op.wb.calendar_list_events", list_calendar_events)
    register_op("op.wb.calendar_get_event", get_calendar_event)
    register_op("op.wb.calendar_coverage", calendar_coverage)
    # Writes — heavy per-change consent lives in the capability layer.
    register_op("op.wb.calendar_create_event", create_calendar_event)
    register_op("op.wb.calendar_update_event", update_calendar_event)
    register_op("op.wb.calendar_delete_event", delete_calendar_event)


_register()
