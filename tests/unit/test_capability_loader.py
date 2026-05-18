"""Unit tests for the capability loader (``work_buddy.knowledge.capability_loader``).

The loader resolves inert ``kind: "capability"`` declarations (those carrying
an ``op`` field) against the Op registry and emits dispatchable ``Capability``
objects. Tests inject a synthetic store so they exercise resolution logic
without depending on the live knowledge store.
"""

from __future__ import annotations

import pytest

from work_buddy.knowledge.capability_loader import (
    SCHEMA_VERSION,
    load_declared_capabilities,
    validate_signature,
)
from work_buddy.knowledge.model import CapabilityUnit, SystemUnit
from work_buddy.mcp_server import op_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    op_registry.clear_ops()
    yield
    op_registry.clear_ops()


def _sample_op(task_id, done=False):
    return {"task_id": task_id, "done": done}


def _declaration(**overrides) -> CapabilityUnit:
    """A well-formed declaration-based capability unit, overridable per test."""
    fields = dict(
        path="tasks/sample_cap",
        name="Sample Cap",
        description="A sample capability.",
        capability_name="sample_cap",
        category="tasks",
        parameters={
            "task_id": {"type": "str", "description": "Task ID", "required": True},
            "done": {"type": "bool", "description": "Done flag", "required": False},
        },
        op="op.wb.sample",
        schema_version=SCHEMA_VERSION,
    )
    fields.update(overrides)
    return CapabilityUnit(**fields)


# ---------------------------------------------------------------------------
# validate_signature
# ---------------------------------------------------------------------------

class TestValidateSignature:
    def test_matching_signature_no_issues(self):
        params = {"task_id": {}, "done": {}}
        assert validate_signature(params, _sample_op) == []

    def test_declared_param_not_in_signature(self):
        params = {"task_id": {}, "bogus": {}}
        issues = validate_signature(params, _sample_op)
        assert any("bogus" in i for i in issues)

    def test_required_callable_param_omitted_from_declaration(self):
        # _sample_op requires task_id; a declaration that omits it is flagged.
        issues = validate_signature({"done": {}}, _sample_op)
        assert any("task_id" in i for i in issues)

    def test_kwargs_callable_accepts_any_declared_param(self):
        def flexible(**kwargs):
            return kwargs

        assert validate_signature({"anything": {}, "at_all": {}}, flexible) == []

    def test_optional_callable_param_omitted_is_fine(self):
        # ``done`` has a default — a declaration may omit it without issue.
        assert validate_signature({"task_id": {}}, _sample_op) == []


# ---------------------------------------------------------------------------
# load_declared_capabilities
# ---------------------------------------------------------------------------

class TestLoadDeclaredCapabilities:
    def test_clean_resolution(self):
        op_registry.register_op("op.wb.sample", _sample_op)
        store = {"tasks/sample_cap": _declaration()}
        caps, issues = load_declared_capabilities(store)
        assert issues == []
        assert len(caps) == 1
        cap = caps[0]
        assert cap.name == "sample_cap"
        assert cap.callable is _sample_op
        assert cap.op_id == "op.wb.sample"
        assert cap.category == "tasks"

    def test_generated_unit_without_op_is_skipped(self):
        """A capability unit with no ``op`` field is generated/legacy — the
        loader ignores it (it flows through the old registration path)."""
        op_registry.register_op("op.wb.sample", _sample_op)
        generated = _declaration(op="", schema_version="")
        caps, issues = load_declared_capabilities({"tasks/sample_cap": generated})
        assert caps == []
        assert issues == []

    def test_non_capability_units_ignored(self):
        store = {"sys/x": SystemUnit(path="sys/x", name="X", description="x")}
        caps, issues = load_declared_capabilities(store)
        assert caps == [] and issues == []

    def test_missing_op_emits_warning(self):
        # op.wb.sample is never registered.
        store = {"tasks/sample_cap": _declaration()}
        caps, issues = load_declared_capabilities(store)
        assert caps == []
        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert issues[0]["check"] == "capability_op_resolution"
        assert "not registered" in issues[0]["message"]

    def test_unknown_schema_version_emits_warning(self):
        op_registry.register_op("op.wb.sample", _sample_op)
        store = {"tasks/sample_cap": _declaration(schema_version="wb-capability/v99")}
        caps, issues = load_declared_capabilities(store)
        assert caps == []
        assert len(issues) == 1
        assert "schema_version" in issues[0]["message"]

    def test_signature_mismatch_emits_warning_and_skips_dispatch(self):
        op_registry.register_op("op.wb.sample", _sample_op)
        bad = _declaration(parameters={"bogus": {"type": "str", "required": True}})
        caps, issues = load_declared_capabilities({"tasks/sample_cap": bad})
        assert caps == []  # not dispatched — schema disagrees with the op
        assert any("signature mismatch" in i["message"] for i in issues)

    def test_malformed_op_id_emits_warning(self):
        store = {"tasks/sample_cap": _declaration(op="not-an-op-id")}
        caps, issues = load_declared_capabilities(store)
        assert caps == []
        assert any("malformed op ID" in i["message"] for i in issues)
