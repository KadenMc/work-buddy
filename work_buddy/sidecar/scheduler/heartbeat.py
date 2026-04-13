"""Heartbeat exclusion windows.

Prevents jobs and heartbeats from firing during configured quiet periods
(e.g., overnight, weekends).

Ported from ClaudeClaw's exclusion-window logic in start.ts,
simplified with Python's ``zoneinfo``.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ExclusionWindow:
    """A time window during which the scheduler should not fire jobs.

    Times are in HH:MM format (24-hour). Overnight windows are supported
    (e.g., start=23:00, end=08:00).

    Days use ISO weekday numbering: 0=Monday, 6=Sunday.
    If ``days`` is empty, the window applies to all days.
    """

    start: str  # "HH:MM"
    end: str    # "HH:MM"
    days: list[int] | None = None  # None = all days


def parse_exclusion_windows(config_list: list[dict]) -> list[ExclusionWindow]:
    """Parse exclusion windows from config.

    Args:
        config_list: List of dicts with ``start``, ``end``, and optional ``days``.

    Returns:
        List of validated ExclusionWindow objects.
    """
    windows: list[ExclusionWindow] = []
    for entry in config_list:
        start = entry.get("start", "")
        end = entry.get("end", "")
        if not _validate_time(start) or not _validate_time(end):
            logger.warning("Invalid exclusion window: %s — skipping.", entry)
            continue
        days = entry.get("days")
        if days is not None and not isinstance(days, list):
            days = None
        windows.append(ExclusionWindow(start=start, end=end, days=days))
    return windows


def _validate_time(t: str) -> bool:
    """Check if a string is valid HH:MM format."""
    if not t or len(t) != 5 or t[2] != ":":
        return False
    try:
        h, m = int(t[:2]), int(t[3:])
        return 0 <= h <= 23 and 0 <= m <= 59
    except ValueError:
        return False


def _time_to_minutes(t: str) -> int:
    """Convert HH:MM to minutes since midnight."""
    return int(t[:2]) * 60 + int(t[3:])


def is_excluded(
    now: datetime,
    windows: list[ExclusionWindow],
    timezone: str | None = None,
) -> bool:
    """Check if the current time falls within any exclusion window.

    Args:
        now: Current datetime (timezone-aware recommended).
        windows: List of exclusion windows from config.
        timezone: IANA timezone name. If provided, ``now`` is converted.

    Returns:
        True if the current time is in an excluded period.
    """
    if not windows:
        return False

    if timezone:
        now = now.astimezone(ZoneInfo(timezone))

    current_day = now.weekday()  # 0=Monday, 6=Sunday
    current_minutes = now.hour * 60 + now.minute

    for window in windows:
        # Check day filter
        if window.days is not None and current_day not in window.days:
            continue

        start_m = _time_to_minutes(window.start)
        end_m = _time_to_minutes(window.end)

        if start_m == end_m:
            # Same start and end = all-day exclusion for matching days
            return True

        if start_m < end_m:
            # Normal window (e.g., 09:00-17:00)
            if start_m <= current_minutes < end_m:
                return True
        else:
            # Overnight window (e.g., 23:00-08:00)
            if current_minutes >= start_m or current_minutes < end_m:
                return True

    return False
