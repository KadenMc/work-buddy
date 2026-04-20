"""All registered context sources — wave-1 (structured) + wave-2/3 (markdown wrappers).

Importing this package registers every source with
:mod:`work_buddy.context.registry`. Callers just need
``import work_buddy.context`` which in turn imports this package.

Structured sources (wave 1 — own ``collect``/``render`` logic,
drill-down-ready):
- git, tasks, projects, chrome

Markdown-wrapper sources (wave 2/3 — delegate to legacy
``work_buddy/collectors/*``; emit one item holding the full markdown;
drill-down not implemented):
- obsidian, obsidian_wellness, calendar, day_planner,
  session_activity, chat, message, smart, datacore
"""

from __future__ import annotations

# Import side-effect: each module registers its ContextSource instance
# at import time. Order only matters for which source "wins" if two
# ever pick the same name — none do today.
from work_buddy.context.sources import git  # noqa: F401
from work_buddy.context.sources import tasks  # noqa: F401
from work_buddy.context.sources import projects  # noqa: F401
from work_buddy.context.sources import chrome  # noqa: F401
from work_buddy.context.sources import obsidian  # noqa: F401
from work_buddy.context.sources import calendar  # noqa: F401
from work_buddy.context.sources import day_planner  # noqa: F401
from work_buddy.context.sources import session_activity  # noqa: F401
from work_buddy.context.sources import chat  # noqa: F401
from work_buddy.context.sources import message  # noqa: F401
from work_buddy.context.sources import smart  # noqa: F401
from work_buddy.context.sources import datacore  # noqa: F401
