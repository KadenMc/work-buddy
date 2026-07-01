"""Unit tests for ``execution_runner`` runtime parameter binding.

The binding that fills in ``thread_id`` for thread-scoped actions is what
keeps journal/email route actions from failing with "missing 1 required
positional argument: 'thread_id'". It is driven by the declared parameter
schema plus the ``is_action`` gate, so the binding logic is exercised here
with fake registry entries, and a declaration-level guard confirms the real
journal/email action declarations actually carry the schema the binding
keys on (otherwise the fix would silently do nothing).
"""

from __future__ import annotations

from types import SimpleNamespace

from work_buddy.threads import execution_runner, models


def _entry(*, is_action: bool, params: dict):
    """Minimal stand-in for a registry ``Capability`` entry — only the
    attributes ``_bind_runtime_parameters`` reads."""
    return SimpleNamespace(is_action=is_action, parameters=params)


class TestBindRuntimeThreadId:
    def test_binds_thread_id_for_action_declaring_it(self):
        t = models.Thread()
        entry = _entry(
            is_action=True,
            params={"thread_id": {"type": "str", "required": True}},
        )
        out = execution_runner._bind_runtime_parameters(
            capability_name="journal_route_to_tasks",
            thread=t, provided={}, entry=entry,
        )
        assert out["thread_id"] == t.thread_id

    def test_always_overrides_provided_thread_id(self):
        # thread_id is runtime-bound: the host thread is authoritative, so
        # a proposal-supplied (possibly LLM-hallucinated) value is replaced.
        t = models.Thread()
        entry = _entry(is_action=True, params={"thread_id": {}})
        out = execution_runner._bind_runtime_parameters(
            capability_name="journal_route_to_tasks",
            thread=t, provided={"thread_id": "journal_backlog"}, entry=entry,
        )
        assert out["thread_id"] == t.thread_id

    def test_skips_when_thread_id_not_declared(self):
        # chrome_tab_* actions declare tab_ids, not thread_id — the
        # binding must not invent a thread_id for them.
        t = models.Thread()
        entry = _entry(is_action=True, params={"tab_ids": {}})
        out = execution_runner._bind_runtime_parameters(
            capability_name="chrome_tab_close",
            thread=t, provided={}, entry=entry,
        )
        assert "thread_id" not in out

    def test_skips_for_non_action_capability(self):
        # Messaging tools declare a non-FSM thread_id but are never
        # dispatched as actions — the is_action gate must exclude them.
        t = models.Thread()
        entry = _entry(is_action=False, params={"thread_id": {}})
        out = execution_runner._bind_runtime_parameters(
            capability_name="send_message",
            thread=t, provided={}, entry=entry,
        )
        assert "thread_id" not in out

    def test_skips_when_entry_unresolved(self):
        t = models.Thread()
        out = execution_runner._bind_runtime_parameters(
            capability_name="journal_route_to_tasks",
            thread=t, provided={}, entry=None,
        )
        assert "thread_id" not in out


class TestRealDeclarationsCarryThreadId:
    """Production-side guard: the actions that broke must actually
    declare ``thread_id`` and be ``is_action`` in the live store, or the
    schema-driven binding never fires for them.
    """

    def test_thread_scoped_actions_declare_thread_id(self):
        from work_buddy.knowledge.capability_loader import (
            load_declared_capabilities,
        )
        from work_buddy.knowledge.store import load_store
        from work_buddy.mcp_server import op_registry

        op_registry.clear_ops()
        op_registry.load_builtin_ops()
        caps, _issues = load_declared_capabilities(load_store())
        by_name = {c.name: c for c in caps}

        # Journal route actions load without optional deps — assert hard.
        for name in (
            "journal_route_to_tasks",
            "journal_route_to_considerations",
            "journal_append_to_note",
        ):
            cap = by_name.get(name)
            assert cap is not None, f"{name} declaration not loaded"
            assert cap.is_action, f"{name} must be is_action"
            assert "thread_id" in cap.parameters, (
                f"{name} must declare thread_id for the binding to fire"
            )

        # Email thread actions carry the identical shape, but their op
        # module may not load in every environment — assert only when
        # present so the test isn't environment-flaky.
        for name in (
            "email_create_tasks",
            "email_close",
            "email_create_umbrella_task",
        ):
            cap = by_name.get(name)
            if cap is None:
                continue
            assert cap.is_action, f"{name} must be is_action"
            assert "thread_id" in cap.parameters, (
                f"{name} must declare thread_id for the binding to fire"
            )
