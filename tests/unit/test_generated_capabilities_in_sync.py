"""Guard: the committed _generated_*.json files match the live registry.

``knowledge/store/_generated_capabilities.json`` and
``_generated_parents.json`` are derived from the MCP capability registry
by ``work_buddy/knowledge/build.py``. They are committed to the repo and
loaded into the knowledge store at runtime — but nothing *forces* a
regeneration when a capability is added. This test makes that drift a
loud failure instead of a silent one: add a capability without
regenerating, and CI fails here with the exact fix command.

## Why this can skip

``build.py`` builds the *unfiltered* registry, importing every
capability category. Some categories pull optional dependencies
(``hindsight_client`` etc.) that are only present in the full
``work-buddy`` environment. In an environment missing them, the build
raises and this test ``skip``s rather than erroring — it still enforces
wherever the complete dependency set is installed (the gateway env, a
properly-provisioned CI runner). Run the suite from the ``work-buddy``
conda env to have this test actually execute:

    conda run -n work-buddy python -m pytest tests/unit/test_generated_capabilities_in_sync.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_STORE = Path(__file__).resolve().parents[2] / "knowledge" / "store"
_REGEN_HINT = (
    "Regenerate from the work-buddy conda env:\n"
    "    conda run -n work-buddy python -m work_buddy.knowledge.build --write\n"
    "then commit knowledge/store/_generated_*.json (and any hand-authored "
    "parent unit reconcile touched)."
)


def _build_or_skip():
    """Build the generated units, or skip if the environment can't.

    Returns ``(capabilities, parent_stubs)``. Skips the test when
    ``build.py`` cannot run here (an optional dependency for some
    capability category is absent — see the module docstring)."""
    try:
        from work_buddy.knowledge.build import (
            build_capability_units,
            build_parent_stubs,
        )
        caps = build_capability_units()
        stubs = build_parent_stubs(caps)
    except Exception as exc:  # RuntimeError from fail-loud build, ImportError…
        pytest.skip(
            f"knowledge.build cannot run in this environment ({exc!s:.160}). "
            f"Run the suite from the work-buddy conda env to enforce this test."
        )
    return caps, stubs


def _committed(name: str) -> dict:
    path = _STORE / name
    if not path.exists():
        pytest.fail(f"{name} is missing from the knowledge store")
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize(obj):
    """Round-trip through JSON so tuples/lists and key order don't cause
    spurious inequality — only genuine content differences remain."""
    return json.loads(json.dumps(obj, sort_keys=True))


def test_generated_capabilities_in_sync():
    """_generated_capabilities.json equals what build.py produces now."""
    caps, _ = _build_or_skip()
    committed = _committed("_generated_capabilities.json")
    if _normalize(committed) != _normalize(caps):
        built_keys = set(caps)
        committed_keys = set(committed)
        missing = sorted(built_keys - committed_keys)
        extra = sorted(committed_keys - built_keys)
        changed = sorted(
            k for k in built_keys & committed_keys
            if _normalize(caps[k]) != _normalize(committed[k])
        )
        pytest.fail(
            "_generated_capabilities.json is out of sync with the registry.\n"
            f"  missing from the file: {missing}\n"
            f"  stale entries in the file: {extra}\n"
            f"  entries that changed: {changed}\n"
            + _REGEN_HINT
        )


def test_generated_parents_in_sync():
    """_generated_parents.json equals what build.py produces now."""
    caps, stubs = _build_or_skip()
    committed = _committed("_generated_parents.json")
    if _normalize(committed) != _normalize(stubs):
        pytest.fail(
            "_generated_parents.json is out of sync with the registry.\n"
            f"  built parent stubs: {sorted(stubs)}\n"
            f"  committed parent stubs: {sorted(committed)}\n"
            + _REGEN_HINT
        )
