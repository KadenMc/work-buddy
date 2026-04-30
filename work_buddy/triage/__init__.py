"""Backwards-compat shim for the Triage→Clarify rename (Slice 3).

The triage subsystem moved to :mod:`work_buddy.clarify` in Slice 3.
This shim re-exports everything so legacy imports keep working without
a hard cutover. New code should ``import from work_buddy.clarify``
directly; this shim exists so external callers, tests, and on-disk
pool snapshots that named the legacy modules don't break overnight.

The shim re-exports:

- :class:`work_buddy.clarify.background.ClarifyPool` (also as
  ``TriagePool``)
- :class:`work_buddy.clarify.background.ClarifyEntry` (also as
  ``PoolEntry``)
- :func:`work_buddy.clarify.background.get_pool` and lifecycle state
  constants
- :data:`work_buddy.clarify.items.TRIAGE_ACTIONS` and
  :data:`work_buddy.clarify.items.TRIAGE_DESTINATIONS`
- :class:`work_buddy.clarify.items.TriageItem` (kept under its legacy
  name; the *items* live alongside the new system but the rename
  scoped to the *pool / capability* surface, not the dataclass).

Submodule shims (``work_buddy.triage.background``,
``work_buddy.triage.items``, ``work_buddy.triage.capabilities.*``) are
available via :mod:`importlib`'s normal lookup because the underlying
``work_buddy.clarify`` modules are loaded with both legacy and new
names registered in :mod:`sys.modules`. See the loop below.
"""

from __future__ import annotations

import importlib
import sys

# Re-export the most commonly-used public surfaces. New callers should
# import from ``work_buddy.clarify`` directly; this is a transitional
# convenience for legacy callers.
from work_buddy.clarify.background import (  # noqa: F401
    BackgroundTriageProducer,
    ClarifyEntry,
    ClarifyPool,
    POOL_ENTRY_STATES,
    PoolEntry,  # alias for ClarifyEntry; declared in background.py
    STATE_DROPPED,
    STATE_PENDING,
    STATE_QUARANTINED,
    STATE_REVIEWED,
    STATE_STALE,
    TriagePool,  # alias for ClarifyPool; declared in background.py
    content_hash,
    get_pool,
    item_content_hash,
)
from work_buddy.clarify.items import (  # noqa: F401
    TRIAGE_ACTIONS,
    TRIAGE_DESTINATIONS,
    TaskMatch,
    TriageCluster,
    TriageItem,
    TriageResult,
)


# ---------------------------------------------------------------------------
# Submodule shims. ``import work_buddy.triage.background`` should resolve
# to the same module as ``import work_buddy.clarify.background`` so that
# patches and isinstance() checks keep working across both names.
# ---------------------------------------------------------------------------

_LEGACY_SUBMODULES = (
    "background",
    "items",
    "card_actions",
    "cluster",
    "config",
    "deadline_extract",
    "detail",
    "dispatch",
    "enrich",
    "execute",
    "presentation",
    "recommend",
    "resolution",
    "sources",
    "sources_triggers",
    "task_match",
    "verdict_call",
    "verdict_schema",
)

for _name in _LEGACY_SUBMODULES:
    try:
        _mod = importlib.import_module(f"work_buddy.clarify.{_name}")
        sys.modules[f"work_buddy.triage.{_name}"] = _mod
    except ImportError:
        # Some submodules may not exist yet (e.g. deadline_extract is
        # Slice 3 new); skip silently so the shim is forward-compat
        # for partial Slice 3 rollouts.
        pass


# Capability submodule shim. ``work_buddy.triage.capabilities.*`` →
# ``work_buddy.clarify.capabilities.*``.
try:
    _cap_pkg = importlib.import_module("work_buddy.clarify.capabilities")
    sys.modules["work_buddy.triage.capabilities"] = _cap_pkg
    for _cap in (
        "inline_triage_scan",
        "journal_triage_scan",
        "triage_pool_quarantine_entry",
        "triage_pool_sweep",
        "triage_review_pool",
        "triage_submit",
    ):
        try:
            _m = importlib.import_module(f"work_buddy.clarify.capabilities.{_cap}")
            sys.modules[f"work_buddy.triage.capabilities.{_cap}"] = _m
        except ImportError:
            pass
except ImportError:
    pass

# Adapter submodule shim.
try:
    _adapt_pkg = importlib.import_module("work_buddy.clarify.adapters")
    sys.modules["work_buddy.triage.adapters"] = _adapt_pkg
    for _ad in ("chrome", "inline", "journal"):
        try:
            _m = importlib.import_module(f"work_buddy.clarify.adapters.{_ad}")
            sys.modules[f"work_buddy.triage.adapters.{_ad}"] = _m
        except ImportError:
            pass
except ImportError:
    pass


__all__ = [
    "BackgroundTriageProducer",
    "ClarifyEntry",
    "ClarifyPool",
    "POOL_ENTRY_STATES",
    "PoolEntry",
    "STATE_DROPPED",
    "STATE_PENDING",
    "STATE_QUARANTINED",
    "STATE_REVIEWED",
    "STATE_STALE",
    "TRIAGE_ACTIONS",
    "TRIAGE_DESTINATIONS",
    "TaskMatch",
    "TriageCluster",
    "TriageItem",
    "TriagePool",
    "TriageResult",
    "content_hash",
    "get_pool",
    "item_content_hash",
]
