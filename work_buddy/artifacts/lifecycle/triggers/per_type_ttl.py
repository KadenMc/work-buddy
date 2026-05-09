"""PerTypeTtl trigger — TTL chosen by the artifact's ``type`` field.

Used by filesystem artifacts: a ``report`` lives 30 days, a ``scratch``
lives 3 days, etc. The mapping is configured at construction.

Records are expected to have a ``type`` field naming the per-type
bucket and an ``expires_at`` ISO timestamp (already computed at write
time using the per-type TTL). The trigger compares ``expires_at`` to
``now`` rather than recomputing — which preserves the original write
intent if the TTL config changes mid-run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from work_buddy.artifacts.expiry import is_expired
from work_buddy.artifacts.protocol import Capability


class PerTypeTtl:
    """TTL determined by record type, applied at write time.

    Args:
        ttl_days_by_type: Mapping ``type → TTL days``. Used at *write*
            time by the consumer; the trigger itself just reads the
            already-computed ``expires_at`` field. The mapping is held
            here for introspection (and so consumers writing through
            this artifact can resolve their TTL declaratively).
        default_ttl_days: TTL for record types not in the mapping.
        expires_field: Field name holding the ISO ``expires_at`` value.
    """

    capabilities = frozenset({Capability.PER_TYPE_TTL})

    def __init__(
        self,
        *,
        ttl_days_by_type: dict[str, int],
        default_ttl_days: int = 14,
        expires_field: str = "expires_at",
    ) -> None:
        self.ttl_days_by_type = dict(ttl_days_by_type)
        self.default_ttl_days = default_ttl_days
        self._expires_field = expires_field

    def is_expired(self, record: dict[str, Any], now: datetime) -> bool:
        return is_expired(record.get(self._expires_field, ""), now=now)

    def ttl_for_type(self, type_name: str) -> int:
        """Return the configured TTL for a type (defaults to default_ttl_days)."""
        return self.ttl_days_by_type.get(type_name, self.default_ttl_days)
