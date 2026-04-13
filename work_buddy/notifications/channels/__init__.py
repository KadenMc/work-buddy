"""Backward-compatibility shim — use work_buddy.notifications.surfaces instead.

This package is deprecated. All channel classes have been renamed to surfaces:
    - NotificationChannel -> NotificationSurface (surfaces.base)
    - ObsidianChannel -> ObsidianSurface (surfaces.obsidian)
"""

# Re-export for any code that hasn't been updated yet
from work_buddy.notifications.surfaces.base import NotificationSurface as NotificationChannel  # noqa: F401
from work_buddy.notifications.surfaces.obsidian import ObsidianSurface as ObsidianChannel  # noqa: F401
