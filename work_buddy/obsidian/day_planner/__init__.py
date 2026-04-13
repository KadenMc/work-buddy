"""Day Planner plugin integration via Obsidian eval_js bridge.

Provides plan reading/writing and gap-filling schedule generation.
Plan entries are scheduling artifacts, not canonical tasks.

Modules:
- env.py: Plugin readiness, plan I/O, resync triggers
- planner.py: Gap-filling schedule generation logic
"""

# Read API
from work_buddy.obsidian.day_planner.env import (  # noqa: F401
    check_ready,
    get_todays_plan,
    trigger_resync,
)

# Write API (uses consent-gated bridge.write_file)
from work_buddy.obsidian.day_planner.env import (  # noqa: F401
    write_plan,
)

# Plan generation (pure logic, no side effects)
from work_buddy.obsidian.day_planner.planner import (  # noqa: F401
    generate_plan,
)
