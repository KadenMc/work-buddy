"""Canonical Journal-day identity and timezone-aware window construction.

A Journal day is identified by the local civil date on which its configured
boundary occurs.  With the default ``05:00`` boundary, for example, the
Journal day ``2026-07-14`` spans ``2026-07-14 05:00`` through
``2026-07-15 05:00`` in the configured Work Buddy timezone.

This module is deliberately independent of the Settings store.  Callers bind
an explicit timezone and boundary, which lets persisted days retain the
policy under which they were created and keeps deterministic fixtures stable.
Window ends are built from the next *civil date*, never by adding 24 elapsed
hours, so spring-forward and fall-back days resolve correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


LOCAL_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_DAY_BOUNDARY = "05:00"


class InvalidLocalTime(ValueError):
    """Raised when a setting value is not a canonical ``HH:MM`` local time."""


def parse_local_time(value: str) -> time:
    """Parse a canonical 24-hour ``HH:MM`` value.

    The strict shape is intentional: settings revisions and day identifiers
    should not depend on permissive parser normalization such as ``5:0``.
    """
    if not isinstance(value, str) or LOCAL_TIME_PATTERN.fullmatch(value) is None:
        raise InvalidLocalTime("day boundary must be a 24-hour time in HH:MM form")
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def format_local_time(value: time) -> str:
    """Return the canonical ``HH:MM`` representation of ``value``."""
    return f"{value.hour:02d}:{value.minute:02d}"


def _coerce_zone(zone: ZoneInfo | str) -> ZoneInfo:
    return zone if isinstance(zone, ZoneInfo) else ZoneInfo(zone)


def _coerce_boundary(boundary: time | str) -> time:
    return boundary if isinstance(boundary, time) else parse_local_time(boundary)


def _roundtrip(candidate: datetime, zone: ZoneInfo) -> datetime:
    """Round-trip a local candidate through UTC to expose gaps/folds."""
    return candidate.astimezone(timezone.utc).astimezone(zone)


def resolve_local_datetime(
    local_date: date,
    local_time: time | str,
    zone: ZoneInfo | str,
) -> datetime:
    """Resolve one civil wall time using Temporal's ``compatible`` policy.

    - A repeated (fall-back) time selects the earlier instant.
    - A nonexistent (spring-forward) time shifts forward by the DST gap.

    ``zoneinfo`` intentionally does not reject nonexistent local datetimes, so
    both folds are round-tripped through UTC and classified explicitly.
    """
    tz = _coerce_zone(zone)
    wall_time = _coerce_boundary(local_time)
    naive = datetime.combine(local_date, wall_time)

    candidates: list[datetime] = []
    forward_gap_candidates: list[datetime] = []
    for fold in (0, 1):
        attached = naive.replace(tzinfo=tz, fold=fold)
        resolved = _roundtrip(attached, tz)
        resolved_naive = resolved.replace(tzinfo=None)
        if resolved_naive == naive:
            candidates.append(resolved)
        elif resolved_naive > naive:
            # For a gap, the pre-transition offset round-trips to the wall
            # time shifted forward by exactly the gap (Temporal compatible).
            forward_gap_candidates.append(resolved)

    if candidates:
        # Normal times collapse to one instant.  During a fold, choose the
        # earlier occurrence by comparing actual UTC instants, not fold flags.
        return min(candidates, key=lambda value: value.astimezone(timezone.utc))

    if forward_gap_candidates:
        return min(
            forward_gap_candidates,
            key=lambda value: value.replace(tzinfo=None) - naive,
        )

    # Defensive fallback for unusual historical timezone transitions. Search
    # forward by civil minutes until the first representable wall time. This
    # preserves the compatible "move forward" direction without 24h math.
    probe = naive
    for _ in range(24 * 60):
        probe += timedelta(minutes=1)
        for fold in (0, 1):
            attached = probe.replace(tzinfo=tz, fold=fold)
            resolved = _roundtrip(attached, tz)
            if resolved.replace(tzinfo=None) == probe:
                return resolved
    raise ValueError(f"could not resolve local datetime {naive!s} in {tz.key}")


@dataclass(frozen=True)
class JournalDayWindow:
    """One half-open Journal day window ``[start, end)``."""

    local_date: date
    timezone: str
    boundary: str
    start: datetime
    end: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "local_date": self.local_date.isoformat(),
            "timezone": self.timezone,
            "day_boundary_start": self.boundary,
            "window_start": self.start.isoformat(),
            "window_end": self.end.isoformat(),
        }


def day_for_instant(
    instant: datetime,
    zone: ZoneInfo | str,
    boundary: time | str = DEFAULT_DAY_BOUNDARY,
) -> date:
    """Return the Journal-day local date that owns ``instant``."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    tz = _coerce_zone(zone)
    boundary_time = _coerce_boundary(boundary)
    local = instant.astimezone(tz)
    owner = local.date()
    # A wall-clock comparison is wrong inside DST gaps and folds. Resolve the
    # boundary to its real compatible-policy instant, then compare on the
    # global timeline. During fall-back, for example, the second 01:15 occurs
    # *after* the selected first 01:30 boundary despite its smaller wall time.
    resolved_boundary = resolve_local_datetime(owner, boundary_time, tz)
    if instant.astimezone(timezone.utc) < resolved_boundary.astimezone(timezone.utc):
        owner -= timedelta(days=1)
    return owner


def window_for_local_date(
    local_date: date,
    zone: ZoneInfo | str,
    boundary: time | str = DEFAULT_DAY_BOUNDARY,
) -> JournalDayWindow:
    """Construct the DST-correct window for a Journal-day identity."""
    tz = _coerce_zone(zone)
    boundary_time = _coerce_boundary(boundary)
    boundary_text = format_local_time(boundary_time)
    start = resolve_local_datetime(local_date, boundary_time, tz)
    end = resolve_local_datetime(local_date + timedelta(days=1), boundary_time, tz)
    return JournalDayWindow(
        local_date=local_date,
        timezone=tz.key,
        boundary=boundary_text,
        start=start,
        end=end,
    )


def next_safe_boundary_transition(
    observed_at: datetime,
    zone: ZoneInfo | str,
    current_boundary: time | str,
    pending_boundary: time | str,
) -> datetime:
    """Return the next instant at which a boundary change is identity-safe.

    Applying a later new boundary at the old, earlier boundary can make the
    current Journal date move *backward*.  Applying an earlier new boundary at
    that new boundary can make it jump *forward*.  Waiting for the later of the
    two wall times guarantees both policies name the same local date at the
    transition instant.  The occurrence itself uses the compatible DST rule.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    tz = _coerce_zone(zone)
    current = _coerce_boundary(current_boundary)
    pending = _coerce_boundary(pending_boundary)
    later = max(current, pending)
    local_now = observed_at.astimezone(tz)
    candidate = resolve_local_datetime(local_now.date(), later, tz)
    if candidate.astimezone(timezone.utc) <= observed_at.astimezone(timezone.utc):
        candidate = resolve_local_datetime(local_now.date() + timedelta(days=1), later, tz)
    return candidate
