"""Consent / policy hook at the processor boundary.

The one behavioral change the backbone introduces: consent is enforced **between the dispatcher and ``Processor.run()``**, not
only inside individual capabilities. Capabilities still *declare* their
``consent_weight``; this boundary *enforces* it.

``policy_check`` returns one of ``"allow" | "deny" | "prompt"``:
- ``action is None``      → ``"allow"`` (the processor declares no gate)
- a live grant exists      → ``"allow"``
- no grant / cannot resolve → ``"prompt"`` (the consumer waits; re-checked next
  drain tick once the user approves)

``"deny"`` is reserved for source ``allowed_actions`` scoping (once event sources
are implemented), where a source can hard-deny an action it isn't scoped for. ``consent_weight="high"``
always re-prompts even inside an approved workflow (the cache enforces this).
"""

from __future__ import annotations

import logging
from typing import Literal

from work_buddy.events.protocol import RunContext

logger = logging.getLogger(__name__)

Decision = Literal["allow", "deny", "prompt"]


def policy_check(
    action: str | None,
    ctx: RunContext,
    *,
    consent_weight: str = "low",
) -> Decision:
    """Gate a processor invocation. See module docstring for the contract."""
    if action is None:
        return "allow"
    try:
        from work_buddy.consent import _cache

        granted = _cache.is_granted(action, consent_weight=consent_weight)
    except Exception as exc:  # pragma: no cover — defensive
        # Fail safe: if consent can't be resolved, hold the event (prompt)
        # rather than silently delivering it.
        logger.warning(
            "events.policy: consent check for %r failed (%s); holding (prompt)",
            action,
            exc,
        )
        return "prompt"
    return "allow" if granted else "prompt"
