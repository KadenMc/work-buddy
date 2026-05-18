"""Unit tests for the corpus-wide checks in ``work_buddy.knowledge.validate``.

These tests focus on individual check functions in isolation by
constructing synthetic stores. Cross-file integration / live-store
tests live in ``test_dev_document_validate_step.py``.
"""

from __future__ import annotations

import pytest

from work_buddy.knowledge.capability_loader import SCHEMA_VERSION
from work_buddy.knowledge.model import CapabilityUnit, DirectionsUnit
from work_buddy.knowledge.validate import (
    _check_capability_op_resolution,
    _check_placeholder_duplicates,
    validate_store,
)
from work_buddy.mcp_server import op_registry


class TestPlaceholderDuplicateCheck:
    """``placeholder_duplicate`` is a hard error — duplicate
    placeholders inside a single unit's ``content.full`` produce zero
    new content at read time (subsequent references render as
    back-reference markers), so they're never the right authorial
    choice. The editor rejects them at write time; this corpus-wide
    check catches any that slipped in via direct JSON edits.
    """

    def test_no_duplicates_returns_empty(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b>> and <<wb:c>>"},
        )
        store = {"a": a}
        assert _check_placeholder_duplicates(store) == []

    def test_duplicate_target_in_one_unit_flagged(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b>> mid <<wb:b>>"},
        )
        store = {"a": a}
        errors = _check_placeholder_duplicates(store)
        assert len(errors) == 1
        err = errors[0]
        assert err["check"] == "placeholder_duplicate"
        assert err["path"] == "a"
        assert "'b'" in err["message"]
        assert "2" in err["message"]

    def test_recursive_flag_does_not_save_a_duplicate(self):
        """Even ``<<wb:b --recursive>>`` twice in one unit is a
        duplicate. The flag doesn't change the read-time semantics
        of the per-unit-occurrence cap."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b --recursive>> some <<wb:b --recursive>>"},
        )
        store = {"a": a}
        errors = _check_placeholder_duplicates(store)
        assert len(errors) == 1
        assert errors[0]["path"] == "a"

    def test_same_target_across_different_units_is_fine(self):
        """The check is per-unit. Unit A and unit B may each reference
        ``<<wb:c>>``; that's not a duplicate."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:c>>"},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "<<wb:c>>"},
        )
        store = {"a": a, "b": b}
        assert _check_placeholder_duplicates(store) == []

    def test_multiple_distinct_duplicates_each_flagged(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={
                "full": "<<wb:x>> <<wb:y>> <<wb:x>> <<wb:y>> <<wb:y>>",
            },
        )
        store = {"a": a}
        errors = _check_placeholder_duplicates(store)
        assert len(errors) == 2
        flagged = {e["message"] for e in errors}
        # x appears twice
        assert any("'x'" in m and "2" in m for m in flagged)
        # y appears three times
        assert any("'y'" in m and "3" in m for m in flagged)

    def test_units_without_placeholders_skipped(self):
        """Plain prose units shouldn't pay the cost of placeholder
        parsing."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "Plain text only, no markers."},
        )
        store = {"a": a}
        assert _check_placeholder_duplicates(store) == []

    def test_empty_content_full_skipped(self):
        """Units missing ``content.full`` shouldn't crash the check."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={},
        )
        store = {"a": a}
        assert _check_placeholder_duplicates(store) == []


class TestCapabilityOpResolutionCheck:
    """``capability_op_resolution`` resolves declaration-based capability
    units against the Op registry and reports failures as *warnings* — the
    direct and declaration-based registration paths coexist, so an unresolved
    declaration is surfaced without failing the store.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        op_registry.clear_ops()
        yield
        op_registry.clear_ops()

    def _declaration(self, **overrides) -> CapabilityUnit:
        fields = dict(
            path="tasks/sample_cap",
            name="Sample Cap",
            description="A sample capability.",
            capability_name="sample_cap",
            category="tasks",
            parameters={"x": {"type": "str", "required": True}},
            op="op.wb.sample",
            schema_version=SCHEMA_VERSION,
        )
        fields.update(overrides)
        return CapabilityUnit(**fields)

    def test_no_declarations_returns_empty(self):
        store = {"a": DirectionsUnit(path="a", name="A", description="a")}
        assert _check_capability_op_resolution(store) == []

    def test_unresolved_declaration_flagged_as_warning(self):
        # op.wb.sample is never registered.
        store = {"tasks/sample_cap": self._declaration()}
        issues = _check_capability_op_resolution(store)
        assert len(issues) == 1
        assert issues[0]["check"] == "capability_op_resolution"
        assert issues[0]["severity"] == "warning"

    def test_resolved_declaration_produces_no_issue(self):
        def sample_op(x):
            return x

        op_registry.register_op("op.wb.sample", sample_op)
        store = {"tasks/sample_cap": self._declaration()}
        assert _check_capability_op_resolution(store) == []


class TestValidateStoreSeverity:
    """``validate_store`` splits blocking errors from non-blocking warnings."""

    def test_return_shape_has_severity_buckets(self):
        result = validate_store()
        for key in ("passed", "failed", "warnings", "errors", "issues", "summary"):
            assert key in result, f"missing key {key!r}"
        assert isinstance(result["passed"], bool)
        # warnings never count toward failure
        assert result["failed"] == len(result["errors"])
        # every error has error severity (default); warnings are separate
        for err in result["errors"]:
            assert err.get("severity", "error") != "warning"
