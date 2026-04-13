"""Google Calendar integration via Obsidian's Google Calendar plugin.

Provides programmatic access to Google Calendar data through the
plugin's runtime API (eval_js bridge). Read operations for context
collection, write operations for event management.

Modules:
- env.py: Calendar API (readiness, queries, mutations — all via eval_js)
"""

# Read API
from work_buddy.calendar.env import (  # noqa: F401
    check_ready,
    get_calendars,
    get_events,
    get_today_schedule,
)

# Write API (all consent-gated, risk: high)
from work_buddy.calendar.env import (  # noqa: F401
    create_event,
    create_event_note,
    delete_event,
    update_event,
)
