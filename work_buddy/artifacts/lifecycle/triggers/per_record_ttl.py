"""PerRecordTtl trigger — each record carries its own ``expires_at``.

The most common pattern. Used by:
    * llm_cache, segmentation_cache (caches with per-entry TTL)
    * messaging (rows with created_at + configured TTL)
    * llm_queue (rows with completed_at + configured TTL)
    * notifications (records with explicit expires_at)

Two operating modes:

1. **Direct** — a record carries an ``expires_at`` ISO field already.
   The trigger reads it directly. (Caches and notifications use this.)

2. **Computed** — a record carries a creation/completion timestamp
   (``ttl_field``) and the trigger computes
   ``expires_at = ttl_field + default_ttl_days``. Used for messaging
   and llm_queue where the table doesn't store expires_at directly.

Boundary-inclusive comparison via :func:`is_expired` — the bug that
spawned this whole refactor (t-96e45c67) is structurally unfixable
here because the comparison lives in one centralized helper.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from work_buddy.artifacts.expiry import _parse_to_utc, is_expired
from work_buddy.artifacts.protocol import Capability


class PerRecordTtl:
    """Each record's expiry is decided by a per-record field.

    Args:
        ttl_field: Field on each record holding either the absolute
            ``expires_at`` ISO timestamp (mode 1) or the creation
            timestamp from which expiry is computed (mode 2). Examples:
            ``"expires_at"`` (mode 1), ``"created_at"`` (mode 2),
            ``"completed_at"`` (mode 2).
        default_ttl_days: When given, treats ``ttl_field`` as a
            creation timestamp and computes
            ``expiry = ttl_field + default_ttl_days``. When ``None``,
            treats ``ttl_field`` as the absolute ``expires_at``.
    """

    capabilities = frozenset({Capability.PER_RECORD_TTL})

    def __init__(
        self,
        *,
        ttl_field: str = "expires_at",
        default_ttl_days: float | None = None,
    ) -> None:
        self._ttl_field = ttl_field
        self._default_ttl_days = default_ttl_days

    def is_expired(self, record: dict[str, Any], now: datetime) -> bool:
        raw = record.get(self._ttl_field, "")
        if not raw:
            return False  # no deadline — treated as never expires

        if self._default_ttl_days is None:
            # Mode 1: raw IS the expires_at
            return is_expired(str(raw), now=now)

        # Mode 2: raw is a creation timestamp; compute expiry
        created = _parse_to_utc(str(raw))
        if created is None:
            return False
        expires = created + timedelta(days=self._default_ttl_days)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return expires <= now
