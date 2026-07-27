"""Best-effort recovery of abandoned Co-work persistence operations."""

from __future__ import annotations

import logging
from typing import Any

from work_buddy.cowork import bootstrap, materialization, reimport
from work_buddy.truth.store import TruthStore


logger = logging.getLogger(__name__)


def recover_store_persistence(store: TruthStore) -> dict[str, Any]:
    """Recover restart-visible Co-work persistence intents for one store."""

    result: dict[str, Any] = {}
    for name, recover in (
        ("bootstrap", bootstrap.recover_bootstrap_intents),
        ("materialization", materialization.recover_materializations),
        ("reimport", reimport.recover_reimport_intents),
    ):
        try:
            result[name] = recover(store)
        except Exception as exc:  # noqa: BLE001 - opening remains available
            logger.warning("Co-work %s recovery scan failed: %s", name, exc)
            result[name] = {"error": str(exc)}
    return result


__all__ = ["recover_store_persistence"]
