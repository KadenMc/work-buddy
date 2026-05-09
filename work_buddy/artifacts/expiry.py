"""UTC-aware expiry primitives shared across all backends.

Centralizes two concerns that were previously copy-pasted into every
cache module (and got the same off-by-one bug copy-pasted with them):

1. **Boundary-inclusive comparison.** An entry whose deadline is exactly
   ``now()`` has used up its lifetime and should be treated as expired.
   Strict ``<`` missed boundary cases where put-then-get happened within
   a single clock tick. We use ``<=`` here, with no other call site
   needing to make the choice.

2. **UTC-aware vs naive ISO timestamps.** New code stores
   ``expires_at`` as UTC-aware ISO (``...+00:00``). Legacy entries
   (caches from before this refactor) store naive ISO. The comparison
   tolerates both: a naive stored value is *treated as UTC* for the
   comparison, which is consistent with how ``datetime.now()`` produced
   it on the writer side.

User- and agent-facing display is handled separately by
:func:`format_for_user`, which converts UTC-aware values to the
configured display timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Internal: parse stored ISO into a UTC-aware datetime
# ---------------------------------------------------------------------------


def _parse_to_utc(stored_iso: str) -> datetime | None:
    """Parse a stored ISO timestamp into a UTC-aware ``datetime``.

    Returns ``None`` if the string is empty or unparseable. Naive
    timestamps are treated as UTC (the historical writer convention).
    """
    if not stored_iso:
        return None
    try:
        dt = datetime.fromisoformat(stored_iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Legacy naive value — treat as UTC, matching the writer side.
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Public: expiry helpers
# ---------------------------------------------------------------------------


def is_expired(stored_iso: str, now: datetime | None = None) -> bool:
    """Return True if a record stored with ``expires_at = stored_iso`` has expired.

    Boundary-inclusive: an entry whose deadline equals ``now`` has expired.

    Args:
        stored_iso: The ``expires_at`` value as written by the producer.
            Empty string or unparseable → returns False (not expired,
            because we have no deadline to compare against).
        now: Override the current time for testing. When ``None``, uses
            ``datetime.now(timezone.utc)``.
    """
    expires = _parse_to_utc(stored_iso)
    if expires is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return expires <= current


def expires_at_iso(now: datetime | None = None, ttl_days: float = 0) -> str:
    """Compute a UTC-aware ``expires_at`` ISO timestamp.

    Args:
        now: Override the base time for testing. When ``None``, uses
            ``datetime.now(timezone.utc)``.
        ttl_days: Lifetime in days. Fractional days are allowed; for
            minute-scale TTLs use ``ttl_days = minutes / 1440``.

    Returns the timestamp in UTC-aware ISO format (``...+00:00``).
    """
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    else:
        base = base.astimezone(timezone.utc)
    return (base + timedelta(days=ttl_days)).isoformat()


# ---------------------------------------------------------------------------
# Public: user-facing display
# ---------------------------------------------------------------------------


def _user_timezone() -> ZoneInfo:
    """Resolve the user's display timezone from config.

    Reads ``display.timezone`` from ``config.yaml`` (with optional
    ``config.local.yaml`` overlay). Falls back to local system timezone
    if not configured.
    """
    try:
        from work_buddy.config import load_config
    except ImportError:
        return ZoneInfo("UTC")
    try:
        cfg = load_config()
        tz_name = cfg.get("display", {}).get("timezone")
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass
    # Fall back to system local time.
    try:
        local_now = datetime.now().astimezone()
        if local_now.tzinfo is not None:
            tzname = str(local_now.tzinfo)
            return ZoneInfo(tzname) if tzname != "None" else ZoneInfo("UTC")
    except Exception:
        pass
    return ZoneInfo("UTC")


def format_for_user(
    dt: datetime | str,
    *,
    fmt: str = "%Y-%m-%d %H:%M:%S %Z",
    tz: ZoneInfo | str | None = None,
) -> str:
    """Format a datetime for user/agent display in their local timezone.

    Args:
        dt: A ``datetime`` or an ISO-formatted string. Naive values are
            treated as UTC (consistent with stored convention).
        fmt: ``strftime`` format string. Default includes the TZ
            abbreviation so the user always sees the zone explicitly.
        tz: Override the display timezone (a ``ZoneInfo`` or its name).
            When ``None``, reads from config.

    Use this anywhere a datetime is shown to the user or an agent.
    Never pass raw ``isoformat()`` of an internal datetime to a UI or
    LLM prompt — agents that get the wrong timezone confidently misread
    the clock and act on it.
    """
    if isinstance(dt, str):
        parsed = _parse_to_utc(dt)
        if parsed is None:
            return dt  # unparseable; surface verbatim
        dt = parsed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    if tz is None:
        target = _user_timezone()
    elif isinstance(tz, str):
        target = ZoneInfo(tz)
    else:
        target = tz

    return dt.astimezone(target).strftime(fmt)
