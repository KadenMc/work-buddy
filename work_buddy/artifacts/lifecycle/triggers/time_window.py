"""TimeWindow trigger — drop records older than ``now - window_days``.

Used by:
    * chrome_ledger (snapshots older than 7 days)
    * escalations_log (records older than 30 days)
    * claude_code_usage (turns older than 90 days)

The trigger parses a per-record timestamp field; records whose
timestamp is older than the cutoff are marked expired. Records lacking
the field are kept (no deadline → no expiry).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from work_buddy.artifacts.expiry import _parse_to_utc
from work_buddy.artifacts.protocol import Capability


class TimeWindow:
    """Records are expired if their timestamp is older than the window.

    Args:
        timestamp_field: Field name carrying the per-record timestamp
            (ISO string). Examples: ``"timestamp"`` (escalations log),
            ``"captured_at"`` (chrome ledger), or whatever column name
            the consumer's table uses.
        window_days: Lifetime in days. Records whose timestamp is
            ``< now - window_days`` are expired.
    """

    capabilities = frozenset({Capability.TIME_WINDOW})

    def __init__(self, *, timestamp_field: str, window_days: float) -> None:
        self._timestamp_field = timestamp_field
        self._window_days = window_days

    def is_expired(self, record: dict[str, Any], now: datetime) -> bool:
        raw = record.get(self._timestamp_field, "")
        if not raw:
            return False
        ts = _parse_to_utc(str(raw))
        if ts is None:
            return False
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(days=self._window_days)
        # Strict < here matches the historical behavior of the existing
        # window-based pruners (chrome_ledger, escalations_log) which
        # used ``ts < cutoff``. The TTL boundary discussion that
        # produced t-96e45c67's <= fix applies to per-record TTL where
        # an entry's deadline equals exactly its computed expires_at;
        # window cutoffs are different — equality means "right at the
        # edge of the keep zone," and we keep those.
        return ts < cutoff
