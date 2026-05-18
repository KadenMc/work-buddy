"""Unit tests for the Op registry (``work_buddy.mcp_server.op_registry``).

The Op registry is the Core lookup table that capability declarations resolve
their ``op`` field against — see the data-first-capabilities refactor.
"""

from __future__ import annotations

import pytest

from work_buddy.mcp_server import op_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test gets an empty registry; restore nothing afterward."""
    op_registry.clear_ops()
    yield
    op_registry.clear_ops()


def _noop(**kwargs):
    return kwargs


class TestRegisterAndLookup:
    def test_register_then_get(self):
        op_registry.register_op("op.wb.sample", _noop)
        assert op_registry.get_op("op.wb.sample") is _noop

    def test_get_unregistered_returns_none(self):
        assert op_registry.get_op("op.wb.missing") is None

    def test_list_ops_sorted(self):
        op_registry.register_op("op.wb.zebra", _noop)
        op_registry.register_op("op.wb.alpha", _noop)
        assert op_registry.list_ops() == ["op.wb.alpha", "op.wb.zebra"]

    def test_clear_ops_empties_registry(self):
        op_registry.register_op("op.wb.sample", _noop)
        op_registry.clear_ops()
        assert op_registry.list_ops() == []


class TestIdValidation:
    @pytest.mark.parametrize("op_id", [
        "op.wb.task_read",
        "op.dev.alice.email_send",          # reverse-DNS third-party
        "op.wb.a.b.c",
    ])
    def test_valid_ids_accepted(self, op_id):
        assert op_registry.is_valid_op_id(op_id)
        op_registry.register_op(op_id, _noop)

    @pytest.mark.parametrize("op_id", [
        "task_read",                        # no op. prefix
        "op.wb",                            # no name segment
        "op..task_read",                    # empty namespace
        "op.WB.task_read",                  # uppercase
        "op.wb.task-read",                  # hyphen not allowed
        "",
    ])
    def test_malformed_ids_rejected(self, op_id):
        assert not op_registry.is_valid_op_id(op_id)
        with pytest.raises(ValueError):
            op_registry.register_op(op_id, _noop)


class TestDuplicateAndReplace:
    def test_duplicate_registration_raises(self):
        op_registry.register_op("op.wb.sample", _noop)
        with pytest.raises(ValueError):
            op_registry.register_op("op.wb.sample", _noop)

    def test_replace_true_overrides(self):
        def other(**kwargs):
            return None

        op_registry.register_op("op.wb.sample", _noop)
        op_registry.register_op("op.wb.sample", other, replace=True)
        assert op_registry.get_op("op.wb.sample") is other

    def test_non_callable_rejected(self):
        with pytest.raises(ValueError):
            op_registry.register_op("op.wb.sample", "not-callable")


class TestBuiltinOps:
    def test_load_builtin_ops_registers_task_read(self):
        """The built-in ops package registers ``op.wb.task_read``."""
        op_registry.load_builtin_ops()
        assert op_registry.get_op("op.wb.task_read") is not None

    def test_load_builtin_ops_idempotent(self):
        op_registry.load_builtin_ops()
        first = op_registry.list_ops()
        op_registry.load_builtin_ops()  # second call must not raise
        assert op_registry.list_ops() == first
