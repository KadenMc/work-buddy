"""Tests for the mode-aware capability registry.

Covers: the mode registry (declarations + lookups + load-time validation),
``available_when`` gate resolution on capability/workflow declarations, the
session ``active_modes`` plumbing, the ``mode_toggle`` op, and the
``wb_search`` / ``wb_run`` mode-gating behavior.
"""

from __future__ import annotations

import pytest

from work_buddy.modes.registry import (
    ModeDef,
    _load_modes,
    get_known_mode_ids,
    get_mode_def,
)


def _write_mode(d, name: str, body: str) -> None:
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Mode registry
# ---------------------------------------------------------------------------

class TestModeRegistry:
    def test_known_modes_include_dev_and_knowledge(self):
        assert {"dev", "knowledge"} <= get_known_mode_ids()

    def test_get_mode_def_returns_modedef(self):
        dev = get_mode_def("dev")
        assert isinstance(dev, ModeDef)
        assert dev.id == "dev"
        assert dev.activatable_when is None

    def test_get_mode_def_unknown_is_none(self):
        assert get_mode_def("nope_not_a_mode") is None

    def test_load_modes_parses_activatable_when(self, tmp_path):
        _write_mode(tmp_path, "exploration", "id: exploration\nlabel: Exploration\ndescription: x\n")
        _write_mode(tmp_path, "paper", "id: paper\nlabel: Paper\ndescription: x\nactivatable_when: '!exploration'\n")
        modes = _load_modes(tmp_path)
        assert set(modes) == {"exploration", "paper"}
        assert modes["paper"].activatable_when == "!exploration"

    def test_compound_activatable_when_against_known_ids(self, tmp_path):
        for m in ("dev", "knowledge", "admin"):
            _write_mode(tmp_path, m, f"id: {m}\nlabel: {m}\ndescription: x\n")
        _write_mode(tmp_path, "cothink", "id: cothink\nlabel: C\ndescription: x\nactivatable_when: '(dev & knowledge) | admin'\n")
        modes = _load_modes(tmp_path)
        assert modes["cothink"].activatable_when == "(dev & knowledge) | admin"

    def test_unknown_mode_in_activatable_when_fails(self, tmp_path):
        _write_mode(tmp_path, "paper", "id: paper\nlabel: Paper\ndescription: x\nactivatable_when: 'nonexistent'\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)

    def test_bad_dsl_in_activatable_when_fails(self, tmp_path):
        _write_mode(tmp_path, "paper", "id: paper\nlabel: Paper\ndescription: x\nactivatable_when: 'a & & b'\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)

    def test_invalid_mode_id_fails(self, tmp_path):
        _write_mode(tmp_path, "bad", "id: bad-id\nlabel: Bad\ndescription: x\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)

    def test_missing_id_fails(self, tmp_path):
        _write_mode(tmp_path, "noid", "label: No ID\ndescription: x\n")
        with pytest.raises(ValueError):
            _load_modes(tmp_path)


# ---------------------------------------------------------------------------
# CapabilityUnit / WorkflowUnit serialization of available_when
# ---------------------------------------------------------------------------

class TestUnitSerialization:
    def test_capability_available_when_round_trips(self):
        from work_buddy.knowledge.model import CapabilityUnit
        unit = CapabilityUnit(
            path="x", name="n", description="d",
            capability_name="c", category="cat",
            available_when="dev & knowledge",
        )
        assert unit._kind_fields()["available_when"] == "dev & knowledge"

    def test_ungated_capability_omits_available_when(self):
        from work_buddy.knowledge.model import CapabilityUnit
        unit = CapabilityUnit(
            path="x", name="n", description="d",
            capability_name="c", category="cat",
        )
        assert "available_when" not in unit._kind_fields()

    def test_workflow_available_when_round_trips(self):
        from work_buddy.knowledge.model import WorkflowUnit
        unit = WorkflowUnit(
            path="x", name="n", description="d",
            workflow_name="w", available_when="knowledge",
        )
        assert unit._kind_fields()["available_when"] == "knowledge"


# ---------------------------------------------------------------------------
# available_when resolution in the capability loader
# ---------------------------------------------------------------------------

def _modes_fixture_op(**kwargs):
    return {"ok": True}


@pytest.fixture(scope="module", autouse=True)
def _register_fixture_op():
    from work_buddy.mcp_server import op_registry
    op_registry.load_builtin_ops()
    try:
        op_registry.register_op("op.test.modes_fixture", _modes_fixture_op)
    except Exception:
        pass  # already registered earlier in this session
    yield


def _cap_store(available_when):
    from work_buddy.knowledge.model import CapabilityUnit
    unit = CapabilityUnit(
        path="test/test_cap_modes",
        name="Test Cap Modes",
        description="fixture capability for mode-gate tests",
        capability_name="test_cap_modes",
        category="test",
        op="op.test.modes_fixture",
        schema_version="wb-capability/v1",
        parameters={},
        available_when=available_when,
    )
    return {"test/test_cap_modes": unit}


class TestAvailableWhenResolution:
    def test_gated_capability_resolves_gate(self):
        from work_buddy.knowledge.capability_loader import load_declared_capabilities
        from work_buddy.control.gates import Component
        caps, issues = load_declared_capabilities(_cap_store("dev"))
        cap = next(c for c in caps if c.name == "test_cap_modes")
        assert cap.available_when == Component("dev")
        assert not any(i["path"] == "test/test_cap_modes" for i in issues)

    def test_ungated_capability_has_none(self):
        from work_buddy.knowledge.capability_loader import load_declared_capabilities
        caps, _ = load_declared_capabilities(_cap_store(None))
        cap = next(c for c in caps if c.name == "test_cap_modes")
        assert cap.available_when is None

    def test_unknown_mode_gate_omitted_with_issue(self):
        from work_buddy.knowledge.capability_loader import load_declared_capabilities
        caps, issues = load_declared_capabilities(_cap_store("totally_unknown_mode"))
        assert not any(c.name == "test_cap_modes" for c in caps)
        assert any(
            i["path"] == "test/test_cap_modes" and "available_when" in i["message"]
            for i in issues
        )

    def test_bad_dsl_gate_omitted_with_issue(self):
        from work_buddy.knowledge.capability_loader import load_declared_capabilities
        caps, issues = load_declared_capabilities(_cap_store("a & & b"))
        assert not any(c.name == "test_cap_modes" for c in caps)
        assert any(i["path"] == "test/test_cap_modes" for i in issues)


# ---------------------------------------------------------------------------
# Session active_modes plumbing
# ---------------------------------------------------------------------------

class TestSessionActiveModes:
    def test_default_empty(self, tmp_agents_dir):
        from work_buddy.agent_session import get_active_modes
        assert get_active_modes() == set()

    def test_set_and_get(self, tmp_agents_dir):
        from work_buddy.agent_session import get_active_modes, set_active_modes
        set_active_modes({"dev", "knowledge"})
        assert get_active_modes() == {"dev", "knowledge"}

    def test_persisted_as_sorted_list(self, tmp_agents_dir):
        import json
        from work_buddy.agent_session import get_session_dir, set_active_modes
        set_active_modes({"knowledge", "dev"})
        manifest = json.loads(
            (get_session_dir() / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["active_modes"] == ["dev", "knowledge"]

    def test_update_preserves_other_fields(self, tmp_agents_dir):
        import json
        from work_buddy.agent_session import get_session_dir, set_active_modes
        manifest_path = get_session_dir() / "manifest.json"
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        set_active_modes({"dev"})
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert after["session_id"] == before["session_id"]
        assert after["active_modes"] == ["dev"]

    def test_empty_set_clears(self, tmp_agents_dir):
        from work_buddy.agent_session import get_active_modes, set_active_modes
        set_active_modes({"dev"})
        set_active_modes(set())
        assert get_active_modes() == set()


# ---------------------------------------------------------------------------
# mode_toggle op
# ---------------------------------------------------------------------------

class TestModeToggle:
    def test_enable_explicit(self, tmp_agents_dir):
        from work_buddy.mcp_server.ops.modes_ops import mode_toggle
        r = mode_toggle("dev", active=True)
        assert r["active"] is True
        assert r["previous"] is False
        assert r["active_modes"] == ["dev"]

    def test_flip_none(self, tmp_agents_dir):
        from work_buddy.mcp_server.ops.modes_ops import mode_toggle
        r1 = mode_toggle("dev", active=None)
        assert r1["active"] is True and r1["previous"] is False
        r2 = mode_toggle("dev", active=None)
        assert r2["active"] is False and r2["previous"] is True

    def test_disable_explicit(self, tmp_agents_dir):
        from work_buddy.mcp_server.ops.modes_ops import mode_toggle
        mode_toggle("dev", active=True)
        r = mode_toggle("dev", active=False)
        assert r["active"] is False
        assert r["active_modes"] == []

    def test_idempotent_enable(self, tmp_agents_dir):
        from work_buddy.mcp_server.ops.modes_ops import mode_toggle
        mode_toggle("dev", active=True)
        r = mode_toggle("dev", active=True)
        assert r["active"] is True
        assert r["previous"] is True

    def test_multiple_modes_accumulate(self, tmp_agents_dir):
        from work_buddy.mcp_server.ops.modes_ops import mode_toggle
        mode_toggle("dev", active=True)
        r = mode_toggle("knowledge", active=True)
        assert set(r["active_modes"]) == {"dev", "knowledge"}

    def test_unknown_mode_denied(self, tmp_agents_dir):
        from work_buddy.mcp_server.ops.modes_ops import mode_toggle
        r = mode_toggle("not_a_real_mode", active=True)
        assert r["denied_by"] == "unknown_mode"
        assert "error" in r

    def test_activation_constraint_denial(self, tmp_agents_dir, monkeypatch):
        from work_buddy.mcp_server.ops import modes_ops
        from work_buddy.modes.registry import ModeDef
        from work_buddy.agent_session import set_active_modes
        fake = ModeDef(id="paper", label="Paper", description="x", activatable_when="!exploration")
        monkeypatch.setattr(
            "work_buddy.modes.registry.get_mode_def",
            lambda mid: fake if mid == "paper" else None,
        )
        set_active_modes({"exploration"})
        r = modes_ops.mode_toggle("paper", active=True)
        assert r["denied_by"] == "activation_constraint"
        assert r["constraint"] == "!exploration"

    def test_activation_constraint_satisfied(self, tmp_agents_dir, monkeypatch):
        from work_buddy.mcp_server.ops import modes_ops
        from work_buddy.modes.registry import ModeDef
        fake = ModeDef(id="paper", label="Paper", description="x", activatable_when="!exploration")
        monkeypatch.setattr(
            "work_buddy.modes.registry.get_mode_def",
            lambda mid: fake if mid == "paper" else None,
        )
        r = modes_ops.mode_toggle("paper", active=True)
        assert r["active"] is True
        assert "paper" in r["active_modes"]


# ---------------------------------------------------------------------------
# wb_search / wb_run mode gating (pure helpers the gateway wires in)
# ---------------------------------------------------------------------------

def _gated_entry(available_when):
    """Minimal stand-in for a registry entry carrying a resolved gate."""
    from work_buddy.control.gates import parse_gate

    class _Entry:
        pass

    e = _Entry()
    e.name = "x"
    e.available_when = parse_gate(available_when) if available_when else None
    return e


class TestModeGating:
    # --- mode_gate_denial (wb_run path) ---
    def test_ungated_entry_passes(self):
        from work_buddy.mcp_server.registry import mode_gate_denial
        assert mode_gate_denial(_gated_entry(None), set()) is None

    def test_gated_entry_denied_when_mode_off(self):
        from work_buddy.mcp_server.registry import mode_gate_denial
        d = mode_gate_denial(_gated_entry("dev"), set())
        assert d is not None
        assert d["denied_by"] == "mode_gate"
        assert d["required_modes"] == ["dev"]
        assert d["active_modes"] == []

    def test_gated_entry_passes_when_mode_on(self):
        from work_buddy.mcp_server.registry import mode_gate_denial
        assert mode_gate_denial(_gated_entry("dev"), {"dev"}) is None

    def test_compound_gate(self):
        from work_buddy.mcp_server.registry import mode_gate_denial
        e = _gated_entry("dev & knowledge")
        assert mode_gate_denial(e, {"dev"}) is not None
        assert mode_gate_denial(e, {"dev", "knowledge"}) is None
        assert mode_gate_denial(e, set())["required_modes"] == ["dev", "knowledge"]

    def test_negation_gate(self):
        from work_buddy.mcp_server.registry import mode_gate_denial
        e = _gated_entry("!exploration")
        assert mode_gate_denial(e, set()) is None
        assert mode_gate_denial(e, {"exploration"}) is not None

    # --- filter_results_by_modes (wb_search path) ---
    def test_filter_hides_gated_shows_ungated_and_unknown(self):
        from work_buddy.mcp_server.registry import filter_results_by_modes
        entries = {"gated": _gated_entry("dev"), "ungated": _gated_entry(None)}
        results = [{"name": "gated"}, {"name": "ungated"}, {"name": "unknown"}]
        out = filter_results_by_modes(results, set(), lambda n: entries.get(n))
        assert {r["name"] for r in out} == {"ungated", "unknown"}

    def test_filter_shows_gated_when_mode_on(self):
        from work_buddy.mcp_server.registry import filter_results_by_modes
        entries = {"gated": _gated_entry("dev")}
        out = filter_results_by_modes([{"name": "gated"}], {"dev"}, lambda n: entries.get(n))
        assert [r["name"] for r in out] == ["gated"]


# ---------------------------------------------------------------------------
# Workflow-side available_when resolution (log-and-ungate, no issues channel)
# ---------------------------------------------------------------------------

class TestWorkflowGateResolution:
    def test_valid_gate_resolves(self):
        from work_buddy.mcp_server.registry import _resolve_mode_gate
        from work_buddy.control.gates import Component
        assert _resolve_mode_gate("dev", "store:x") == Component("dev")

    def test_none_and_empty_return_none(self):
        from work_buddy.mcp_server.registry import _resolve_mode_gate
        assert _resolve_mode_gate(None, "store:x") is None
        assert _resolve_mode_gate("", "store:x") is None

    def test_unknown_mode_ungates(self):
        # Workflows log + leave ungated (vs capabilities, which omit + issue).
        from work_buddy.mcp_server.registry import _resolve_mode_gate
        assert _resolve_mode_gate("totally_unknown_mode", "store:x") is None

    def test_bad_dsl_ungates(self):
        from work_buddy.mcp_server.registry import _resolve_mode_gate
        assert _resolve_mode_gate("a & & b", "store:x") is None
