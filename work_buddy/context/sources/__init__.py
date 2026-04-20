"""Wave-1 context sources: git, tasks, projects, chrome.

Importing this package registers every source with
:mod:`work_buddy.context.registry` — pull it in from wherever context
collection is wired up (e.g., the MCP capability layer, the triage
capabilities, morning routine) so sources are available.

Wave 2/3 sources (obsidian, calendar, day_planner, session_activity,
chat, message, datacore) land in phase 6.
"""

from __future__ import annotations

# Import side-effect: each module registers its ContextSource instance
# at import time. Order only matters for which source "wins" if two
# ever pick the same name — none do today.
from work_buddy.context.sources import git  # noqa: F401
from work_buddy.context.sources import tasks  # noqa: F401
from work_buddy.context.sources import projects  # noqa: F401
from work_buddy.context.sources import chrome  # noqa: F401
