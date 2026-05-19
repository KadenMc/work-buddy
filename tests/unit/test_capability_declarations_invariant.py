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
    """``load_declared_capabilities`` returns no warnings against the live store."""
    issues = loaded["issues"]
    assert issues == [], (
        f"{len(issues)} resolution issue(s) against the live store: "
        f"{[(i['path'], i['message']) for i in issues[:5]]}"
    )


def test_resolved_capability_count_matches_unit_count(loaded) -> None:
    """Every capability unit resolves; the loader drops none."""
    unit_count = len(_capability_units(loaded["store"]))
    resolved_count = len(loaded["caps"])
    assert resolved_count == unit_count, (
        f"{unit_count} capability unit(s) in the store but only "
        f"{resolved_count} resolved — the missing ones likely point at an "
        f"op that is not registered."
    )


def test_every_op_id_is_registered(loaded) -> None:
    """Every declaration's ``op`` resolves to a registered Op callable."""
    unregistered = [
        (u.path, u.op)
        for u in _capability_units(loaded["store"])
        if u.op and op_registry.get_op(u.op) is None
    ]
    assert unregistered == [], (
        f"{len(unregistered)} declaration(s) name an op that is not "
        f"registered: {unregistered[:5]}"
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
