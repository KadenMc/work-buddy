"""MtimeWindow trigger — drop based on filesystem mtime.

Used by:
    * agent_sessions (session dirs with stale manifest + no recent
      file activity)
    * logs/global (log files older than max_age_days)

The agent_sessions consumer needs both the manifest's ``created_at``
*and* the freshest mtime in the directory tree to be old. That's a
"compound" trigger — the per-record mtime check is augmented by an
``activity_check`` callable that the consumer supplies. The DirectoryTree
backend pre-computes ``_latest_mtime`` (for SESSION_DIRS shape) so the
trigger has the data without having to crawl again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from work_buddy.artifacts.expiry import _parse_to_utc
from work_buddy.artifacts.protocol import StorageTrait


class MtimeWindow:
    """Filesystem-mtime-based expiry, with optional activity check.

    Args:
        mtime_field: Field name carrying the per-record mtime ISO
            string. For DirectoryTreeStorage(LOG_FILES), this is
            ``"_mtime"``; for SESSION_DIRS, the consumer should pass
            either ``"created_at"`` (manifest field) or ``"_latest_mtime"``
            (freshest activity).
        max_age_days: Records whose mtime is older than
            ``now - max_age_days`` are expired.
        activity_check: Optional callable that takes a record dict and
            returns ``True`` if there's recent activity (in which case
            the trigger keeps the record despite its mtime). For
            agent_sessions: ``lambda r: <_latest_mtime>>= cutoff``.
    """

    capabilities = frozenset({StorageTrait.MTIME_WINDOW})

    def __init__(
        self,
        *,
        mtime_field: str,
        max_age_days: float,
        activity_check: Callable[[dict[str, Any], datetime], bool] | None = None,
    ) -> None:
        self._mtime_field = mtime_field
        self._max_age_days = max_age_days
        self._activity_check = activity_check

    def is_expired(self, record: dict[str, Any], now: datetime) -> bool:
        raw = record.get(self._mtime_field, "")
        if not raw:
            return False
        mtime = _parse_to_utc(str(raw))
        if mtime is None:
            return False
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(days=self._max_age_days)
        if mtime >= cutoff:
            return False
        # Mtime is past cutoff. If an activity check says there's still
        # recent activity, override the expiry decision.
        if self._activity_check is not None and self._activity_check(record, cutoff):
            return False
        return True
