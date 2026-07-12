"""Shared timestamp formatting for context collectors and activity rendering.

The single home for "render a UTC instant as the user's local wall-clock time".
Context bundles are read by the journal agent on one local-time timeline, so
every collector must agree on the timezone it prints. The convention is
**local-naive**: convert to ``config.USER_TZ`` then drop the tzinfo, matching the
journal's own naive-local Log entries.

``config.USER_TZ`` is read **inside** each function (call time), never at import,
so importing this module stays cheap and does not pull config off disk — the same
discipline ``config.py``'s lazy ``USER_TZ`` getter exists to preserve.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Relative-window shorthand shared by every capability that takes a time bound
# (chrome_activity, activity_timeline, hot_files, the context-bundle window, …):
# an integer amount + a unit whose first letter is m / h / d.
_RELATIVE_RE = re.compile(r"\s*(\d+)\s*(m|min|h|hour|hours|d|day|days)\s*", re.IGNORECASE)
_RELATIVE_UNIT = {"m": "minutes", "h": "hours", "d": "days"}


def parse_time_bound(
    value: str | datetime | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Parse one end of a time window to an aware-UTC datetime.

    The single canonical parser for the work-buddy window vocabulary used
    across capability declarations and the context pipeline:

      * **relative shorthand** — ``"2h"``, ``"30m"``, ``"1d"`` (also
        ``min`` / ``hour(s)`` / ``day(s)``): resolved as ``now - delta``.
      * **ISO datetime** — ``"2026-07-07T10:40:00"`` or with an offset / ``Z``.
        A **naive** ISO string is read as the user's local wall-clock time —
        the journal / collector convention, matching the strings
        ``read_journal_state`` emits — and converted to UTC. An offset-aware
        string is converted to UTC as-is.

    ``now`` defaults to the current instant; a naive ``now`` is treated as UTC.
    Returns an aware-UTC datetime, or ``None`` when *value* is falsy or
    unparseable, so callers can forward an optional bound without pre-checking.

    Note the deliberate asymmetry with :func:`to_local_naive`, which assumes a
    naive datetime is *UTC*: that function renders UTC collector output to local
    time, whereas this one parses a user-typed bound, which is local.
    """
    if value is None or value == "":
        return None

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    if isinstance(value, str):
        rel = _RELATIVE_RE.fullmatch(value)
        if rel:
            unit = _RELATIVE_UNIT[rel.group(2)[0].lower()]
            return now - timedelta(**{unit: int(rel.group(1))})

    dt = parse_iso(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        from work_buddy.config import USER_TZ

        dt = dt.replace(tzinfo=USER_TZ)
    return dt.astimezone(timezone.utc)


def parse_iso(value: str | datetime | None) -> datetime | None:
    """Parse an ISO timestamp (tolerating a trailing ``Z``) to a datetime.

    Passes a ``datetime`` through unchanged and maps falsy / unparseable input
    to ``None``, so callers can feed it raw cache values without pre-checking.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def to_local_naive(dt: datetime | None) -> datetime | None:
    """Convert a datetime to the user's local timezone, tzinfo stripped.

    ``None`` passes through. A naive datetime is assumed to already be UTC
    (every collector source is UTC-aware or UTC-derived), making this a total
    function. The result is naive local wall-clock time — consistent with the
    journal's Log entries and git's local commit timestamps.
    """
    if dt is None:
        return None
    from datetime import timezone

    from work_buddy.config import USER_TZ

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(USER_TZ).replace(tzinfo=None)


def format_local(
    value: str | datetime | None,
    fmt: str = "%Y-%m-%d %H:%M",
    *,
    fallback: str = "",
) -> str:
    """Render *value* (ISO string or datetime) as local-naive wall-clock time.

    Returns *fallback* when *value* is absent or unparseable.
    """
    local = to_local_naive(parse_iso(value))
    if local is None:
        return fallback
    return local.strftime(fmt)


def format_session_span(
    start: str | datetime | None,
    end: str | datetime | None,
    *,
    fallback: str = "",
    empty: str = "",
) -> str:
    """Render when a session happened, from its start/end instants, in local time.

    Same-day spans collapse to ``YYYY-MM-DD HH:MM–HH:MM``; cross-day spans show
    both dates. With only one endpoint, renders that instant. With neither,
    returns *fallback* if given, else *empty* — the only difference between the
    two historical renderers this replaces (chat used ``""``, the session
    summary used ``"—"``).
    """
    s = to_local_naive(parse_iso(start))
    e = to_local_naive(parse_iso(end))
    if s and e:
        if s.date() == e.date():
            return f"{s.strftime('%Y-%m-%d %H:%M')}–{e.strftime('%H:%M')}"
        return f"{s.strftime('%Y-%m-%d %H:%M')}–{e.strftime('%Y-%m-%d %H:%M')}"
    if s:
        return s.strftime("%Y-%m-%d %H:%M")
    if e:
        return e.strftime("%Y-%m-%d %H:%M")
    return fallback or empty
