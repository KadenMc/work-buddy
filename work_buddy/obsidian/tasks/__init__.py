"""Obsidian Tasks plugin integration via eval_js bridge.

Provides programmatic access to the Tasks plugin (v7.23.1) cache
for reading, markdown-level mutations for plugin-owned data, and
a SQLite metadata store for work-buddy-owned data (state, urgency,
complexity, contract link, etc.).

Modules:
- env.py: Read API (query, count, search via plugin cache)
- store.py: Metadata store (SQLite, keyed by task ID)
- mutations.py: Write API (creates store records + writes markdown)
- manager.py: Intelligence (briefing, stale check, focus suggestions)
"""

# Read API (plugin cache)
from work_buddy.obsidian.tasks.env import (  # noqa: F401
    check_ready,
    get_focused_tasks,
    get_inbox_tasks,
    get_master_list_tasks,
    get_overdue_tasks,
    get_task_counts,
    query_tasks,
    search_tasks,
)

# Write API (store + markdown)
from work_buddy.obsidian.tasks.mutations import (  # noqa: F401
    archive_completed,
    assign_task,
    create_task,
    delete_task,
    toggle_task,
    strip_legacy_tags_from_line,
    update_task,
    verify_task,
)

# Metadata store
from work_buddy.obsidian.tasks.store import (  # noqa: F401
    counts_by_state,
    get as get_task_metadata,
    get_history as get_task_history,
    query as query_task_metadata,
)

# Intelligence
from work_buddy.obsidian.tasks.manager import (  # noqa: F401
    daily_briefing,
    get_inbox_count,
    get_tasks_by_state,
    review_inbox,
    stale_check,
    suggest_focus,
    weekly_review_data,
)
