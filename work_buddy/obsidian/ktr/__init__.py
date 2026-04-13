"""Keep the Rhythm (v0.2.8) — writing activity tracking integration.

Provides hot-file scores and per-file writing intensity data from the
Keep the Rhythm Obsidian plugin's runtime activity ledger.
"""

from work_buddy.obsidian.ktr.env import (
    check_ready,
    get_file_activity,
    get_hot_files,
)

__all__ = ["check_ready", "get_hot_files", "get_file_activity"]
