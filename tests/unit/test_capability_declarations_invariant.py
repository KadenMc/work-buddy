"""Live-store invariants for the data-first capability layer.

The capability bulk-migration's definition of done is that every capability
is a *declaration* (a ``kind: "capability"`` knowledge-store unit carrying an
``op`` field) plus a registered Op. These tests are the regression guard: a
future change that adds a declaration without registering its op, registers
an op without a declaration, or breaks the loader's signature validation will
fail here even if no per-category test exists for that capability.
"""

from __future__ import annotations

import pytest

from work_buddy.knowledge.capability_loader import (
    SCHEMA_VERSION,
    load_declared_capabilities,
)
from work_buddy.knowledge.model import CapabilityUnit
from work_buddy.knowledge.store import load_store
from work_buddy.mcp_server import op_registry


@pytest.fixture
def loaded() -> dict:
    """A fresh-from-disk snapshot for the invariant checks."""
    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    store = load_store()
    caps, issues = load_declared_capabilities(store)
    return {"store": store, "caps": caps, "issues": issues}


def _capability_units(store: dict) -> list[CapabilityUnit]:
    return [u for u in store.values() if isinstance(u, CapabilityUnit)]


def _expected_op_module(category: str) -> str:
    """Convention: capabilities of ``category`` are registered by
    ``ops/<category>_ops.py``. Used to identify declarations whose op is
    expected to be unregistered when the corresponding op module failed to
    load (e.g. an optional runtime dependency is missing)."""
    return f"{category}_ops"


def _is_expected_unregistered(unit: CapabilityUnit, failed_modules: set[str]) -> bool:
    """True when ``unit``'s op is unresolved because its op module failed
    to load — an expected per-environment gap, not a regression."""
    return _expected_op_module(unit.category) in failed_modules


def test_every_capability_unit_is_a_declaration(loaded) -> None:
    """Every capability knowledge unit carries an ``op`` field."""
    missing = [
        u.path
        for u in _capability_units(loaded["store"])
        if not u.op
    ]
    assert missing == [], (
        f"{len(missing)} capability unit(s) missing an op field — every "
        f"capability must be a declaration: {missing[:5]}"
    )


def test_every_declaration_uses_the_current_schema_version(loaded) -> None:
    wrong_version = [
        (u.path, u.schema_version)
        for u in _capability_units(loaded["store"])
        if u.schema_version != SCHEMA_VERSION
    ]
    assert wrong_version == [], (
        f"{len(wrong_version)} declaration(s) carry a stale schema_version "
        f"(expected {SCHEMA_VERSION!r}): {wrong_version[:5]}"
    )


def test_loader_resolves_every_declaration_with_zero_issues(loaded) -> None:
    """``load_declared_capabilities`` returns no unexpected warnings.

    A declaration whose op module failed to load (because the host
    environment lacks the module's optional runtime dependency) is allowed
    to surface a ``not registered`` issue — that is the per-environment
    safe-degradation path, not a regression. Every other issue is a bug.
    """
    failed = op_registry.failed_op_modules()
    by_path = {u.path: u for u in _capability_units(loaded["store"])}
    unexpected = [
        i for i in loaded["issues"]
        if not (
            "not registered in the Op registry" in i["message"]
            and i["path"] in by_path
            and _is_expected_unregistered(by_path[i["path"]], failed)
        )
    ]
    assert unexpected == [], (
        f"{len(unexpected)} unexpected resolution issue(s): "
        f"{[(i['path'], i['message']) for i in unexpected[:5]]}"
    )


def test_resolved_capability_count_matches_unit_count(loaded) -> None:
    """Every capability unit resolves, modulo declarations whose op module
    legitimately failed to load in this environment."""
    failed = op_registry.failed_op_modules()
    expected = [
        u for u in _capability_units(loaded["store"])
        if not _is_expected_unregistered(u, failed)
    ]
    resolved_count = len(loaded["caps"])
    assert resolved_count == len(expected), (
        f"{len(expected)} capability unit(s) expected to resolve in this "
        f"environment but only {resolved_count} resolved — the missing "
        f"ones likely point at an op that is not registered. "
        f"Failed op modules this run: {sorted(failed) or 'none'}."
    )


def test_every_op_id_is_registered(loaded) -> None:
    """Every declaration's ``op`` resolves to a registered Op callable —
    except declarations whose op module legitimately failed to load."""
    failed = op_registry.failed_op_modules()
    unregistered = [
        (u.path, u.op)
        for u in _capability_units(loaded["store"])
        if u.op
           and op_registry.get_op(u.op) is None
           and not _is_expected_unregistered(u, failed)
    ]
    assert unregistered == [], (
        f"{len(unregistered)} declaration(s) name an op that is not "
        f"registered (and whose op module did not legitimately fail to "
        f"load): {unregistered[:5]}. Failed op modules this run: "
        f"{sorted(failed) or 'none'}."
    )


def test_op_ids_match_op_namespace_grammar(loaded) -> None:
    """Every ``op`` field is a well-formed ``op.<namespace>.<name>`` id."""
    malformed = [
        (u.path, u.op)
        for u in _capability_units(loaded["store"])
        if not op_registry.is_valid_op_id(u.op)
    ]
    assert malformed == [], (
        f"{len(malformed)} declaration(s) carry a malformed op id: "
        f"{malformed[:5]}"
    )
