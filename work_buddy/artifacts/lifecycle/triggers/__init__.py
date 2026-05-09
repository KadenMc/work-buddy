"""Lifecycle triggers — declarative "is this record expired right now?".

Four triggers, each justified by at least one real consumer:

* :class:`PerTypeTtl` — filesystem artifacts (TTL by artifact type).
* :class:`PerRecordTtl` — caches, messaging, queue, notifications
  (each record carries its own ``expires_at`` field, OR an offset is
  computed from a per-record creation timestamp + a configured TTL).
* :class:`TimeWindow` — chrome ledger, escalations log, claude_code_usage
  (drop records older than ``now - window_days``).
* :class:`MtimeWindow` — agent sessions, logs/global (drop based on
  filesystem mtime, optionally with an activity-check predicate).
"""

from __future__ import annotations

from work_buddy.artifacts.lifecycle.triggers.mtime_window import MtimeWindow
from work_buddy.artifacts.lifecycle.triggers.per_record_ttl import PerRecordTtl
from work_buddy.artifacts.lifecycle.triggers.per_type_ttl import PerTypeTtl
from work_buddy.artifacts.lifecycle.triggers.time_window import TimeWindow

__all__ = ["MtimeWindow", "PerRecordTtl", "PerTypeTtl", "TimeWindow"]
