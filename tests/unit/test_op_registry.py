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


class TestOptionalDependencyWhitelist:
    """``load_builtin_ops`` skips an op module only when its failure matches
    the optional-dependency whitelist; everything else propagates so a real
    bug in an op module surfaces at boot instead of being silently swallowed."""

    def _force_import_error(self, monkeypatch, missing: str, target_module: str = None):
        """Make every import (or just ``target_module``) raise
        ``ModuleNotFoundError(name=missing)`` to simulate an absent dependency.
        """
        import builtins
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if target_module is None or name == target_module:
                raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)

    def _patch_memory_ops_reload(self, monkeypatch, raise_with):
        """Make ``importlib.reload`` raise ``raise_with`` whenever it reloads
        the memory_ops module; other modules reload normally."""
        import importlib

        # Warm up so every ops module is in sys.modules and gets reloaded
        # (rather than imported fresh) on the next load_builtin_ops call.
        op_registry.load_builtin_ops()
        # Clear state so the next load_builtin_ops re-iterates and the
        # already-registered ops don't trigger duplicate-registration errors.
        op_registry.clear_ops()

        real_reload = importlib.reload

        def fake_reload(module):
            if module.__name__.endswith(".memory_ops"):
                raise raise_with
            return real_reload(module)

        monkeypatch.setattr(importlib, "reload", fake_reload)

    def test_whitelisted_missing_dep_records_failure_and_continues(self, monkeypatch):
        """When ``memory_ops`` fails to import because the whitelisted optional
        dep is absent, the module is recorded in ``failed_op_modules`` and the
        load proceeds — other ops still register."""
        self._patch_memory_ops_reload(
            monkeypatch,
            ModuleNotFoundError(
                "No module named 'hindsight_client'", name="hindsight_client",
            ),
        )

        op_registry.load_builtin_ops()  # must not raise

        assert "memory_ops" in op_registry.failed_op_modules()
        assert op_registry.get_op("op.wb.memory_read") is None
        # An unrelated module's ops are still there.
        assert op_registry.get_op("op.wb.task_read") is not None

    def test_non_whitelisted_missing_dep_propagates(self, monkeypatch):
        """A ``ModuleNotFoundError`` whose missing module is NOT whitelisted
        for that op module crashes ``load_builtin_ops`` loudly."""
        self._patch_memory_ops_reload(
            monkeypatch,
            ModuleNotFoundError(
                "No module named 'definitely_not_a_real_optional_dep'",
                name="definitely_not_a_real_optional_dep",
            ),
        )

        with pytest.raises(ModuleNotFoundError):
            op_registry.load_builtin_ops()

    def test_non_import_error_propagates(self, monkeypatch):
        """A non-import error (e.g. a wrong ``register_op`` call) in an op
        module is NOT swallowed by the whitelist."""
        self._patch_memory_ops_reload(
            monkeypatch,
            RuntimeError("simulated bug in memory_ops._register()"),
        )

        with pytest.raises(RuntimeError, match="simulated bug"):
            op_registry.load_builtin_ops()
