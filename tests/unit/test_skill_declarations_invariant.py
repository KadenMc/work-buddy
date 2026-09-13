"""Live-store invariants for the data-first skill layer.

The schema conversion's definition of done is that every skill is a
*declaration* (a ``kind: "skill"`` knowledge-store unit carrying an
``op`` field) plus a registered Op. These tests are the regression guard: a
future change that adds a declaration without registering its op, registers
an op without a declaration, or breaks the loader's signature validation will
fail here even if no per-category test exists for that skill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.knowledge.skill_loader import (
    SCHEMA_VERSION,
    load_declared_skills,
)
from work_buddy.knowledge.file_store import list_unit_paths, read_unit
from work_buddy.knowledge.model import SkillUnit
from work_buddy.knowledge.store import load_store
from work_buddy.mcp_server import op_registry


_STORE_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "store"


@pytest.fixture
def loaded() -> dict:
    """A fresh-from-disk snapshot for the invariant checks."""
    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    store = load_store()
    skills, issues = load_declared_skills(store)
    return {"store": store, "skills": skills, "issues": issues}


def _skill_units(store: dict) -> list[SkillUnit]:
    return [u for u in store.values() if isinstance(u, SkillUnit)]


def _expected_op_module(category: str) -> str:
    """Convention: skills of ``category`` are registered by
    ``ops/<category>_ops.py``. Used to identify declarations whose op is
    expected to be unregistered when the corresponding op module failed to
    load (e.g. an optional runtime dependency is missing)."""
    return f"{category}_ops"


def _is_expected_unregistered(unit: SkillUnit, failed_modules: set[str]) -> bool:
    """True when ``unit``'s op is unresolved because its op module failed
    to load — an expected per-environment gap, not a regression."""
    return _expected_op_module(unit.category) in failed_modules


def test_repository_store_uses_only_canonical_skill_schema() -> None:
    stale: list[tuple[str, str]] = []
    skill_count = 0
    for unit_path in list_unit_paths(_STORE_DIR):
        raw = read_unit(_STORE_DIR, unit_path)
        assert raw is not None
        if raw.get("kind") == "skill":
            skill_count += 1
        if raw.get("kind") == "capability":
            stale.append((unit_path, "kind"))
        if "capability_name" in raw:
            stale.append((unit_path, "capability_name"))
        if raw.get("schema_version") == "wb-capability/v1":
            stale.append((unit_path, "schema_version"))
        if "capabilities" in raw:
            stale.append((unit_path, "capabilities"))

    assert skill_count > 0, "expected repository-owned skill declarations"
    assert stale == [], f"repository store contains stale skill schema fields: {stale[:5]}"


def test_every_skill_unit_is_a_declaration(loaded) -> None:
    """Every skill knowledge unit carries an ``op`` field."""
    missing = [
        u.path
        for u in _skill_units(loaded["store"])
        if not u.op
    ]
    assert missing == [], (
        f"{len(missing)} skill unit(s) missing an op field — every "
        f"skill must be a declaration: {missing[:5]}"
    )


def test_every_declaration_uses_the_current_schema_version(loaded) -> None:
    wrong_version = [
        (u.path, u.schema_version)
        for u in _skill_units(loaded["store"])
        if u.schema_version != SCHEMA_VERSION
    ]
    assert wrong_version == [], (
        f"{len(wrong_version)} declaration(s) carry a stale schema_version "
        f"(expected {SCHEMA_VERSION!r}): {wrong_version[:5]}"
    )


def test_loader_resolves_every_declaration_with_zero_issues(loaded) -> None:
    """``load_declared_skills`` returns no unexpected warnings.

    A declaration whose op module failed to load (because the host
    environment lacks the module's optional runtime dependency) is allowed
    to surface a ``not registered`` issue — that is the per-environment
    safe-degradation path, not a regression. Every other issue is a bug.
    """
    failed = op_registry.failed_op_modules()
    by_path = {u.path: u for u in _skill_units(loaded["store"])}
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


def test_resolved_skill_count_matches_unit_count(loaded) -> None:
    """Every skill unit resolves, modulo declarations whose op module
    legitimately failed to load in this environment."""
    failed = op_registry.failed_op_modules()
    expected = [
        u for u in _skill_units(loaded["store"])
        if not _is_expected_unregistered(u, failed)
    ]
    resolved_count = len(loaded["skills"])
    assert resolved_count == len(expected), (
        f"{len(expected)} skill unit(s) expected to resolve in this "
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
        for u in _skill_units(loaded["store"])
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
        for u in _skill_units(loaded["store"])
        if not op_registry.is_valid_op_id(u.op)
    ]
    assert malformed == [], (
        f"{len(malformed)} declaration(s) carry a malformed op id: "
        f"{malformed[:5]}"
    )
