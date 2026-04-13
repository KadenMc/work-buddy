"""Obsidian vault integration for work-buddy."""

from work_buddy.obsidian.plugins import (
    active_plugins,
    installed_plugins,
    is_active,
    plugin_config,
    require_plugins,
)
from work_buddy.obsidian.plugin_versions import (
    confirm_working,
    diff_versions,
    format_versions_report,
    get_snapshot,
    save_snapshot,
    snapshot_versions,
    INTEGRATED_PLUGINS,
)
from work_buddy.obsidian.commands import ObsidianCommands
from work_buddy.obsidian import bridge

__all__ = [
    "active_plugins",
    "installed_plugins",
    "is_active",
    "plugin_config",
    "require_plugins",
    "confirm_working",
    "diff_versions",
    "format_versions_report",
    "get_snapshot",
    "save_snapshot",
    "snapshot_versions",
    "INTEGRATED_PLUGINS",
    "ObsidianCommands",
    "bridge",
]
