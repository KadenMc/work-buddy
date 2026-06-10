"""Partition registry bootstrap — the ONE place that imports partition providers.

Keeps the engine core domain-free: only this thin wiring module imports the domain
partition adapters to trigger their self-registration (mirrors how ``ir/store._get_source``
imports the IR sources). Each import is defensive — a partition whose domain fails to
import is logged and skipped, never crashing the others.
"""

from __future__ import annotations

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_DONE = False


def ensure_partitions_registered() -> None:
    """Idempotently register all available partitions into the global registry."""
    global _DONE
    if _DONE:
        return

    # knowledge — self-registers on import (the critical path + A/B target).
    try:
        import work_buddy.knowledge.partition  # noqa: F401
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("knowledge partition registration failed: %s", exc)

    # IR sources (conversation, projects, chrome, summary, task_note) via the wrapper.
    try:
        from work_buddy.index.partitions.ir_source import register_ir_partitions
        register_ir_partitions()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("IR partition registration failed: %s", exc)

    # vault chunks — self-registers on import IF present (may be deferred; tolerated).
    try:
        import work_buddy.vault_index.partition  # noqa: F401
    except Exception as exc:
        logger.debug("vault partition not registered (deferred or unavailable): %s", exc)

    _DONE = True
