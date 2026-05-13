"""NeverExpires trigger — records that opt into infinite retention.

Some artifacts must outlive every cleanup tick: durable subsystem state,
audit logs, user-authored data, anything whose loss is more expensive
than its storage cost. ``NeverExpires`` makes that intent explicit at
the type level rather than encoded as a sentinel TTL like 36500 days.

Use the module-level constant :data:`INFINITE_LIFECYCLE` re-exported
from ``work_buddy.artifacts`` for the common case:

.. code-block:: python

    register_artifact(Artifact(
        name="my-durable-store",
        storage=SqliteRowsStorage(...),
        lifecycle=INFINITE_LIFECYCLE,
    ))

A grep for ``INFINITE_LIFECYCLE`` then enumerates every artifact that
opted into infinite retention — auditable in a way that
``lifecycle=None`` (or a 100-year TTL hidden in a constructor) is not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from work_buddy.artifacts.protocol import Capability


class NeverExpires:
    """A trigger whose ``is_expired`` always returns False.

    Composes with any ``ExpiryAction`` (typically :class:`Delete` for
    shape consistency with other lifecycles) but the action will never
    fire because ``Lifecycle.find_expired`` returns an empty list.

    Carries no lifecycle capabilities — the artifact's capability union
    will not advertise any trigger-side flag, which truthfully reflects
    that no expiry policy applies.
    """

    capabilities = frozenset[Capability]()

    def is_expired(self, record: dict[str, Any], now: datetime) -> bool:
        return False
