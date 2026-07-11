"""Activation policy for automatic conversation summaries."""

from __future__ import annotations


COMPONENT_ID = "conversation_summaries"


def summaries_active() -> bool:
    """Return whether automatic conversation summaries should run.

    The user preference is authoritative when explicitly set.  For users
    upgrading from the pre-preference implementation, an explicit legacy
    ``use_incremental`` value is still honored.  A completely undecided
    installation is active by default.
    """
    try:
        from work_buddy.health.preferences import is_wanted

        wanted = is_wanted(COMPONENT_ID)
        if wanted is not None:
            return bool(wanted)
    except Exception:
        pass

    try:
        from work_buddy.config import load_config

        summaries = (
            (load_config().get("conversation_observability") or {})
            .get("summaries", {})
            or {}
        )
        if "use_incremental" in summaries:
            return bool(summaries["use_incremental"])
    except Exception:
        pass

    return True
