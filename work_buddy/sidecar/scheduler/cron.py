"""Cron expression matching.

Ported from ClaudeClaw's cron.ts — standard 5-field cron expressions:
    MINUTE HOUR DAY_OF_MONTH MONTH DAY_OF_WEEK
    0-59   0-23 1-31         1-12  0-6 (0=Sunday)

Uses Python's ``zoneinfo`` for timezone handling instead of
ClaudeClaw's manual UTC-offset shifting.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into the set of matching values.

    Supports: ``*``, ``*/n``, ``n``, ``n-m``, ``n-m/s``, comma-separated.

    Args:
        field: The cron field string (e.g. ``"*/5"``, ``"1-5"``, ``"1,3,5"``).
        min_val: Minimum valid value (inclusive).
        max_val: Maximum valid value (inclusive).

    Returns:
        Set of integer values that match this field.
    """
    result: set[int] = set()

    for part in field.split(","):
        part = part.strip()
        if not part:
            continue

        # Handle step: */n or range/n
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                continue
            if step < 1:
                continue

        if part == "*":
            result.update(range(min_val, max_val + 1, step))
        elif "-" in part:
            try:
                lo, hi = part.split("-", 1)
                lo_val, hi_val = int(lo), int(hi)
            except ValueError:
                continue
            lo_val = max(lo_val, min_val)
            hi_val = min(hi_val, max_val)
            result.update(range(lo_val, hi_val + 1, step))
        else:
            try:
                val = int(part)
            except ValueError:
                continue
            if step > 1:
                # e.g. "5/2" — values starting at 5 with step 2
                result.update(range(val, max_val + 1, step))
            elif min_val <= val <= max_val:
                result.add(val)

    return result


def cron_matches(expr: str, dt: datetime, timezone: str | None = None) -> bool:
    """Check if a cron expression matches the given datetime.

    Args:
        expr: 5-field cron expression string.
        dt: The datetime to check (should be timezone-aware or UTC).
        timezone: IANA timezone name (e.g. ``"America/New_York"``).
                  If provided, ``dt`` is converted to this timezone
                  before matching.

    Returns:
        True if the expression matches.
    """
    if timezone:
        tz = ZoneInfo(timezone)
        dt = dt.astimezone(tz)

    fields = expr.strip().split()
    if len(fields) != 5:
        return False

    minute_f, hour_f, dom_f, month_f, dow_f = fields

    minutes = parse_cron_field(minute_f, 0, 59)
    hours = parse_cron_field(hour_f, 0, 23)
    doms = parse_cron_field(dom_f, 1, 31)
    months = parse_cron_field(month_f, 1, 12)
    dows = parse_cron_field(dow_f, 0, 6)

    # Python: Monday=0, Sunday=6 → Cron: Sunday=0, Saturday=6
    # Convert: (dt.weekday() + 1) % 7
    cron_dow = (dt.weekday() + 1) % 7

    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and cron_dow in dows
    )


def cron_interval_seconds(
    expr: str,
    *,
    timezone: str | None = None,
    sample_after: datetime | None = None,
    sample_count: int = 8,
) -> int | None:
    """Estimate the smallest seconds-between-firings for a schedule.

    Useful for sizing a sane jitter ceiling per schedule. Walks
    ``sample_count`` consecutive matches and returns the minimum gap —
    "minimum" because jitter must not exceed the *smallest* interval,
    or two firings can collide.

    Defaults to a fixed Monday-midnight UTC reference so the result is
    deterministic across calls. Returns ``None`` if the expression
    doesn't parse or fewer than two matches land inside the search
    window (a 1-week-per-step scan).

    For typical schedules (``*/N``, ``0 * * * *``, daily, weekly) every
    gap is identical and the loop is just confirming the value. For
    irregular schedules (``0 9,13 * * *``: 4h then 20h) the loop finds
    the small gap whichever sample window happens to land in.
    """
    if sample_after is None:
        sample_after = datetime(2026, 1, 5, 0, 0, tzinfo=timezone_utc())

    matches: list[datetime] = []
    cursor = sample_after
    for _ in range(sample_count + 1):
        nxt = next_cron_match(expr, cursor, timezone, max_minutes=10080)
        if nxt is None:
            break
        matches.append(nxt)
        cursor = nxt

    if len(matches) < 2:
        return None

    gaps = [
        int((b - a).total_seconds())
        for a, b in zip(matches, matches[1:])
    ]
    return min(g for g in gaps if g > 0) if any(g > 0 for g in gaps) else None


def timezone_utc():
    """tz-aware UTC; small helper to keep the import surface contained."""
    from datetime import timezone as _tz
    return _tz.utc


# Largest jitter we'll suggest as a max, regardless of schedule. Keeps
# the upper bound recognizable and protects daily/weekly jobs from
# accidentally getting 10-minute spread windows where 5 is plenty.
JITTER_MAX_HARD_CAP_SECONDS = 300


def compute_max_jitter_seconds(interval_seconds: int | None) -> int:
    """Suggested ``jitter_seconds`` ceiling for a schedule with this gap.

    Heuristic: roughly ``interval / 10``, clamped to the hard 5-minute
    cap. The 1/10 rule keeps the jittered fire well inside the same
    "logical" bucket — a */5 minute job at +30s is still firmly the
    "every 5 minutes" job, just shifted. Going much higher than that
    starts overlapping with the next interval and stops being a
    spreading mechanism.

    Special handling:

    * ``interval_seconds is None`` (unparseable schedule) → 0. The UI
      can use this to disable the jitter input until the schedule
      parses.
    * Very frequent schedules where ``interval / 10`` is below the
      ~30s scheduler tick — return that tiny value anyway. The user
      sees it in the UI and a hint warns that the value will be
      quantized away by the tick. We don't pretend jitter is unsupported
      there; we just don't pretend it's useful either.

    Round to clean increments (10s) so the displayed cap reads as a
    bound the user might actually pick rather than an unround
    arithmetic byproduct.
    """
    if interval_seconds is None or interval_seconds <= 0:
        return 0
    raw = interval_seconds // 10
    capped = min(raw, JITTER_MAX_HARD_CAP_SECONDS)
    if capped <= 0:
        return 0
    # Round down to nearest 10s for display; ensures the displayed
    # max is always a multiple of 10 (e.g. 18s → 10s, 187s → 180s).
    return (capped // 10) * 10


def next_cron_match(
    expr: str,
    after: datetime,
    timezone: str | None = None,
    max_minutes: int = 2880,
) -> datetime | None:
    """Find the next datetime matching a cron expression.

    Brute-force forward scan, checking each minute up to ``max_minutes``
    (default 2880 = 48 hours).

    Args:
        expr: 5-field cron expression.
        after: Start scanning after this time.
        timezone: IANA timezone name for matching.
        max_minutes: Maximum minutes to scan forward.

    Returns:
        The next matching datetime, or None if not found within the window.
    """
    # Start at the next whole minute
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(max_minutes):
        if cron_matches(expr, candidate, timezone):
            return candidate
        candidate += timedelta(minutes=1)

    return None
