"""Source descriptor registry for the triage pool (Slice 1).

Every entry in the pool was captured from some **source** — a journal
thread, a Chrome tab, an inline Obsidian selection, etc. Sources differ
in lifecycle expectations:

- A journal thread might be re-edited by the user as the day's running
  notes evolve; the original captured text might no longer reflect the
  user's intent.
- A Chrome tab can be closed; once gone, the captured page text is a
  ghost.
- An inline ``send-to-agent`` selection is the user's affirmative
  capture; treating it as transient would erode trust (V2a — capture
  promise integrity).

Rather than hard-coding per-source TTLs and quarantine rules in the
sweeper, each source declares a :class:`SourceDescriptor` here. The
sweep dispatches on the descriptor; new sources register their own
descriptor and inherit the sweep machinery for free.

Override model
--------------

Code defaults live in :data:`_DEFAULT_REGISTRY`. The
:func:`load_source_registry` loader merges per-source overrides from
``triage.pool.sources`` in ``config.yaml`` / ``config.local.yaml``
on top of the defaults — same merge discipline as
:mod:`work_buddy.triage.config`. Users tune one source by writing one
nested block::

    # config.local.yaml
    triage:
      pool:
        sources:
          journal_thread:
            ttl_days: 7

The trigger functions themselves live in
:mod:`work_buddy.triage.sources_triggers` and are dispatched by name.
This keeps the registry data-only (importable from migration scripts
without paying the cost of bridge / vault helpers).

Why a separate file
-------------------

The :mod:`work_buddy.triage.config` file holds **stage-shape** config
(profile names, max_tokens, escalation chain) — knobs about HOW the
LLM call runs. This file holds **lifecycle** config (TTLs, quarantine
triggers) — knobs about how entries AGE in the pool. They overlap
philosophically but cleanly separate by concern: stage-shape is about
producing entries; lifecycle is about retiring them.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Trigger names
# ---------------------------------------------------------------------------

# Quarantine trigger names. Each maps to a function in
# :mod:`work_buddy.triage.sources_triggers`. Keep this list small and
# orthogonal — adding a new trigger means adding a new dispatch arm
# in the sweeper, so casual additions accumulate cost.
TRIGGER_SOURCE_REMOVED = "source_removed"
TRIGGER_SOURCE_EDITED_BEYOND_MATCH = "source_edited_beyond_match"
TRIGGER_TAG_REMOVED = "tag_removed"

KNOWN_TRIGGERS: frozenset[str] = frozenset({
    TRIGGER_SOURCE_REMOVED,
    TRIGGER_SOURCE_EDITED_BEYOND_MATCH,
    TRIGGER_TAG_REMOVED,
})


# ---------------------------------------------------------------------------
# Descriptor dataclass
# ---------------------------------------------------------------------------


@dataclass
class SourceDescriptor:
    """Lifecycle declaration for one capture source.

    Attributes:
        name: Source identifier (matches ``PoolEntry.source`` —
            e.g. ``"journal_thread"``, ``"chrome_tab"``, ``"inline"``).
        ttl_days: Soft expiry. After this many days, the sweep
            transitions ``state`` from ``pending`` to ``stale``.
            ``None`` means no TTL (e.g. inline captures, which are
            user-affirmative and should not auto-expire).
        quarantine_triggers: Ordered list of trigger names. The sweep
            calls each trigger's checker function in order; the first
            one that fires sets the quarantine reason.
        config: Source-specific knobs the trigger functions read
            (e.g. inline's ``capture_tag``; journal's
            ``edit_match_threshold``). Open dict — descriptors can
            store anything their triggers need.
    """

    name: str
    ttl_days: int | None
    quarantine_triggers: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for trigger in self.quarantine_triggers:
            if trigger not in KNOWN_TRIGGERS:
                raise ValueError(
                    f"Source {self.name!r} declares unknown quarantine "
                    f"trigger {trigger!r}. Valid: {sorted(KNOWN_TRIGGERS)}"
                )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Code-side defaults. Overrides under ``triage.pool.sources.<name>``
# in ``config.yaml`` / ``config.local.yaml`` deep-merge on top of these.
_DEFAULT_REGISTRY: dict[str, dict[str, Any]] = {
    # Journal threads — cron-segmented from today's running notes.
    # 5-day TTL; quarantine when the journal file is gone or the
    # captured text no longer matches what's in the file (cosine
    # 0.85, soft check via difflib).
    "journal_thread": {
        "ttl_days": 5,
        "quarantine_triggers": [
            TRIGGER_SOURCE_REMOVED,
            TRIGGER_SOURCE_EDITED_BEYOND_MATCH,
        ],
        "config": {
            "edit_match_threshold": 0.85,
        },
    },

    # Chrome tabs — captured from the live tab ledger.
    # 2-day TTL; quarantine when the tab is no longer in the ledger
    # (the user closed it).
    "chrome_tab": {
        "ttl_days": 2,
        "quarantine_triggers": [TRIGGER_SOURCE_REMOVED],
        "config": {},
    },

    # Inline ``send-to-agent`` captures. User-affirmative — should
    # not auto-expire (ttl_days=None) and should not auto-quarantine
    # on edit. The conservative trigger is ``source_removed`` (the
    # source file no longer exists at all). ``tag_removed`` is
    # available in the registry for the future when inline captures
    # start writing back a confirmation tag (out of scope for Slice 1).
    "inline": {
        "ttl_days": None,
        "quarantine_triggers": [TRIGGER_SOURCE_REMOVED],
        "config": {
            # Reserved for the future tag-removal flow; included now
            # so users overriding inline can flip to ["tag_removed"]
            # without also editing this default block.
            "capture_tag": "wb/captured",
        },
    },
}


# ---------------------------------------------------------------------------
# Loader / module-level singleton
# ---------------------------------------------------------------------------


_REGISTRY_CACHE: dict[str, SourceDescriptor] | None = None


def _build_registry(
    overrides: dict[str, Any],
) -> dict[str, SourceDescriptor]:
    merged: dict[str, dict[str, Any]] = deepcopy(_DEFAULT_REGISTRY)
    for source_name, override_block in (overrides or {}).items():
        if not isinstance(override_block, dict):
            continue
        existing = merged.setdefault(source_name, {
            "ttl_days": None,
            "quarantine_triggers": [],
            "config": {},
        })
        for key, value in override_block.items():
            if (
                key == "config"
                and isinstance(existing.get("config"), dict)
                and isinstance(value, dict)
            ):
                # Deep-merge config so users overriding one config
                # key don't have to restate the whole block.
                existing["config"] = {**existing["config"], **value}
            else:
                existing[key] = value
    return {
        name: SourceDescriptor(
            name=name,
            ttl_days=spec.get("ttl_days"),
            quarantine_triggers=list(spec.get("quarantine_triggers") or []),
            config=dict(spec.get("config") or {}),
        )
        for name, spec in merged.items()
    }


def load_source_registry() -> dict[str, SourceDescriptor]:
    """Return the merged source registry (defaults + user overrides).

    Cached. Call :func:`reset_for_tests` between tests if you've
    monkeypatched config; otherwise the cache is fine for the
    lifetime of the process.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        try:
            from work_buddy.config import load_config
            cfg = (load_config() or {}).get("triage", {}) or {}
        except Exception:
            cfg = {}
        overrides = (cfg.get("pool", {}) or {}).get("sources", {}) or {}
        _REGISTRY_CACHE = _build_registry(overrides)
    return _REGISTRY_CACHE


def get_descriptor(source: str) -> SourceDescriptor | None:
    """Return the descriptor for ``source``, or ``None`` if unknown.

    Returning ``None`` for unknown sources is intentional — the pool
    code treats it as "no TTL, no quarantine triggers" rather than
    crashing. New sources should register a descriptor explicitly.
    """
    return load_source_registry().get(source)


def all_descriptors() -> list[SourceDescriptor]:
    """For the daily sweep — iterate every known source."""
    return list(load_source_registry().values())


def register_source(descriptor: SourceDescriptor) -> None:
    """Register (or replace) a source descriptor at runtime.

    Useful for plugins / experiments that want to add a new source
    without editing this file. Idempotent on ``descriptor.name``.
    """
    registry = load_source_registry()
    registry[descriptor.name] = descriptor


def reset_for_tests() -> None:
    """Test hook — drop the cached registry so the next call rebuilds."""
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None
