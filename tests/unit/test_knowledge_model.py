"""Unit tests for the KnowledgeUnit type hierarchy, context chaining, and placeholders."""

import pytest

from work_buddy.knowledge.model import (
    KnowledgeUnit,
    PromptUnit,
    DirectionsUnit,
    SystemUnit,
    CapabilityUnit,
    WorkflowUnit,
    VaultUnit,
    unit_from_dict,
    validate_dag,
    _resolve_placeholders,
    _extract_placeholder_refs,
)


class TestKnowledgeUnitBase:
    def test_construction(self):
        u = KnowledgeUnit(
            path="test/unit",
            kind="system",
            name="Test",
            description="A test unit",
        )
        assert u.path == "test/unit"
        assert u.scope == "system"
        assert u.context_before == []
        assert u.context_after == []

    def test_scope_field(self):
        u = KnowledgeUnit(
            path="x", kind="system", name="X", description="x",
            scope="personal",
        )
        assert u.scope == "personal"

    def test_tier_index_includes_scope(self):
        u = KnowledgeUnit(
            path="x", kind="system", name="X", description="x",
            context_before=["a/b"],
        )
        t = u.tier("index")
        assert t["scope"] == "system"
        assert t["context_before"] == ["a/b"]

    def test_tier_summary_includes_chains(self):
        u = KnowledgeUnit(
            path="x", kind="system", name="X", description="x",
            context_after=["c/d"],
            content={"summary": "Short text"},
        )
        t = u.tier("summary")
        assert t["context_after"] == ["c/d"]
        assert t["content"] == "Short text"

    def test_to_dict_includes_chains_when_nonempty(self):
        u = KnowledgeUnit(
            path="x", kind="system", name="X", description="x",
            context_before=["a"],
        )
        d = u.to_dict()
        assert d["context_before"] == ["a"]
        assert "context_after" not in d  # empty, should be omitted

    def test_to_dict_omits_empty_chains(self):
        u = KnowledgeUnit(
            path="x", kind="system", name="X", description="x",
        )
        d = u.to_dict()
        assert "context_before" not in d
        assert "context_after" not in d


class TestContextChaining:
    def _make_store(self):
        a = DirectionsUnit(
            path="dev/dev-mode",
            name="Dev Mode",
            description="Enter dev mode",
            content={"full": "You are a developmental agent."},
        )
        b = DirectionsUnit(
            path="dev/retro",
            name="Session Retro",
            description="Critique this session",
            content={"full": "Do the retro steps."},
            context_before=["dev/dev-mode"],
        )
        c = DirectionsUnit(
            path="dev/extra",
            name="Extra",
            description="Extra context",
            content={"full": "Bonus info."},
        )
        d = DirectionsUnit(
            path="dev/both",
            name="Both",
            description="Has before and after",
            content={"full": "Main content."},
            context_before=["dev/dev-mode"],
            context_after=["dev/extra"],
        )
        return {u.path: u for u in [a, b, c, d]}

    def test_full_without_store_no_resolution(self):
        store = self._make_store()
        retro = store["dev/retro"]
        result = retro.tier("full")
        assert "context from:" not in result["content"]
        assert result["content"] == "Do the retro steps."

    def test_full_with_store_prepends_before(self):
        store = self._make_store()
        retro = store["dev/retro"]
        result = retro.tier("full", store=store)
        assert "--- context from: dev/dev-mode ---" in result["content"]
        assert "You are a developmental agent." in result["content"]
        assert "Do the retro steps." in result["content"]
        # Before content should come BEFORE own content
        idx_before = result["content"].index("developmental agent")
        idx_own = result["content"].index("retro steps")
        assert idx_before < idx_own

    def test_full_with_store_appends_after(self):
        store = self._make_store()
        both = store["dev/both"]
        result = both.tier("full", store=store)
        assert "--- context from: dev/extra ---" in result["content"]
        # After content should come AFTER own content
        idx_own = result["content"].index("Main content")
        idx_after = result["content"].index("Bonus info")
        assert idx_own < idx_after

    def test_full_with_store_both_before_and_after(self):
        store = self._make_store()
        both = store["dev/both"]
        result = both.tier("full", store=store)
        assert "developmental agent" in result["content"]
        assert "Main content" in result["content"]
        assert "Bonus info" in result["content"]

    def test_missing_chain_ref_gracefully_skipped(self):
        unit = DirectionsUnit(
            path="test",
            name="Test",
            description="test",
            content={"full": "Own content."},
            context_before=["nonexistent/path"],
        )
        store = {"test": unit}
        result = unit.tier("full", store=store)
        # Should just have own content, no crash
        assert result["content"] == "Own content."

    def test_chain_is_not_recursive(self):
        """A→B and B→C: loading A should include B but NOT C."""
        c = DirectionsUnit(
            path="c", name="C", description="c",
            content={"full": "C content."},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "B content."},
            context_before=["c"],
        )
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "A content."},
            context_before=["b"],
        )
        store = {"a": a, "b": b, "c": c}
        result = a.tier("full", store=store)
        # Should include B's raw content, NOT B's resolved chain (C)
        assert "B content." in result["content"]
        assert "C content." not in result["content"]


class TestPromptUnitHierarchy:
    def test_prompt_unit_is_knowledge_unit(self):
        assert issubclass(PromptUnit, KnowledgeUnit)

    def test_directions_is_prompt_unit(self):
        assert issubclass(DirectionsUnit, PromptUnit)

    def test_existing_subclass_construction(self):
        d = DirectionsUnit(
            path="tasks/triage",
            name="Triage",
            description="Triage tasks",
            trigger="user wants to triage",
        )
        assert d.kind == "directions"
        assert d.scope == "system"  # default
        assert d.trigger == "user wants to triage"


class TestVaultUnit:
    def test_construction(self):
        v = VaultUnit(
            path="personal/metacognition/example-pattern-a",
            name="Example Pattern A",
            description="Test fixture pattern",
            category="work_pattern",
            severity="HIGH",
            last_observed="2026-04-03",
            observation_count=12,
            source_file="work_patterns/example-pattern-a.md",
        )
        assert v.kind == "personal"
        assert v.scope == "personal"
        assert v.category == "work_pattern"
        assert v.severity == "HIGH"
        assert v.observation_count == 12

    def test_vault_unit_is_knowledge_unit_not_prompt_unit(self):
        assert issubclass(VaultUnit, KnowledgeUnit)
        assert not issubclass(VaultUnit, PromptUnit)

    def test_tier_includes_vault_fields(self):
        v = VaultUnit(
            path="p/x",
            name="X",
            description="x",
            category="feedback",
            severity="LOW",
            last_observed="2026-01-01",
            observation_count=3,
            content={"summary": "Short"},
        )
        t = v.tier("summary")
        assert t["category"] == "feedback"
        assert t["severity"] == "LOW"
        assert t["observation_count"] == 3
        assert t["scope"] == "personal"

    def test_search_phrases_includes_category(self):
        v = VaultUnit(
            path="p/x", name="X", description="x",
            category="work_pattern", severity="HIGH",
        )
        phrases = v.search_phrases()
        assert "work pattern" in phrases
        assert "high" in phrases


class TestDeserialization:
    def test_unit_from_dict_personal(self):
        data = {
            "kind": "personal",
            "name": "Test",
            "description": "A test",
            "category": "feedback",
            "severity": "MODERATE",
            "last_observed": "2026-04-12",
            "observation_count": 5,
        }
        u = unit_from_dict("personal/test", data)
        assert isinstance(u, VaultUnit)
        assert u.category == "feedback"
        assert u.observation_count == 5

    def test_unit_from_dict_with_chains(self):
        data = {
            "kind": "directions",
            "name": "Retro",
            "description": "Retro directions",
            "context_before": ["dev/dev-mode"],
        }
        u = unit_from_dict("dev/retro", data)
        assert isinstance(u, DirectionsUnit)
        assert u.context_before == ["dev/dev-mode"]
        assert u.context_after == []

    def test_unit_from_dict_backward_compat(self):
        """Existing JSON without context chains should work fine."""
        data = {
            "kind": "system",
            "name": "Old Unit",
            "description": "No chains",
        }
        u = unit_from_dict("old/unit", data)
        assert u.context_before == []
        assert u.context_after == []


class TestValidateDag:
    def test_warns_on_broken_chain_refs(self):
        u = DirectionsUnit(
            path="a",
            name="A",
            description="a",
            context_before=["nonexistent"],
        )
        errors = validate_dag({"a": u})
        assert any("context_before" in e and "nonexistent" in e for e in errors)

    def test_no_error_on_valid_chains(self):
        a = DirectionsUnit(path="a", name="A", description="a")
        b = DirectionsUnit(
            path="b", name="B", description="b",
            context_before=["a"],
        )
        errors = validate_dag({"a": a, "b": b})
        chain_errors = [e for e in errors if "context_before" in e or "context_after" in e]
        assert chain_errors == []

    def test_cycle_via_children_detected(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            children=["b"],
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            children=["a"],
        )
        errors = validate_dag({"a": a, "b": b})
        assert any("Cycle" in e for e in errors)

    def test_cycle_via_context_after_detected(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            context_after=["b"],
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            context_after=["a"],
        )
        errors = validate_dag({"a": a, "b": b})
        assert any("Cycle" in e for e in errors)

    def test_cycle_via_placeholder_detected(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "See: <<wb:b>>"},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "See: <<wb:a>>"},
        )
        errors = validate_dag({"a": a, "b": b})
        assert any("Cycle" in e for e in errors)

    def test_no_cycle_on_acyclic_placeholders(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "Ref: <<wb:b>>"},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "Just B."},
        )
        errors = validate_dag({"a": a, "b": b})
        assert not any("Cycle" in e for e in errors)


class TestExtractPlaceholderRefs:
    def test_no_placeholders(self):
        assert _extract_placeholder_refs({"full": "Plain text."}) == []

    def test_single_placeholder(self):
        assert _extract_placeholder_refs({"full": "<<wb:obsidian/bridge>>"}) == ["obsidian/bridge"]

    def test_placeholder_with_flags(self):
        refs = _extract_placeholder_refs({"full": "<<wb:tasks/new --recursive>>"})
        assert refs == ["tasks/new"]

    def test_multiple_placeholders(self):
        content = {"full": "<<wb:a>> text <<wb:b --recursive>>"}
        refs = _extract_placeholder_refs(content)
        assert refs == ["a", "b"]

    def test_empty_content(self):
        assert _extract_placeholder_refs({}) == []


class TestResolvePlaceholders:
    def _make_store(self):
        bridge = DirectionsUnit(
            path="obsidian/bridge", name="Bridge", description="bridge",
            content={"full": "Bridge failure protocol content."},
        )
        task_new = DirectionsUnit(
            path="tasks/task-new", name="Task New", description="new task",
            content={"full": "Create a task.\n\n<<wb:obsidian/bridge>>"},
            context_after=["obsidian/bridge"],
        )
        handoff = DirectionsUnit(
            path="tasks/handoff", name="Handoff", description="handoff",
            content={"full": "Write handoff.\n\n<<wb:tasks/task-new --recursive>>\n\nConfirm."},
        )
        standalone = DirectionsUnit(
            path="other", name="Other", description="other",
            content={"full": "No placeholders here."},
        )
        return {u.path: u for u in [bridge, task_new, handoff, standalone]}

    def test_basic_resolution(self):
        store = self._make_store()
        text = "Before.\n<<wb:obsidian/bridge>>\nAfter."
        result = _resolve_placeholders(text, store)
        assert "Bridge failure protocol content." in result
        assert "--- context from: obsidian/bridge ---" in result
        assert "Before." in result
        assert "After." in result

    def test_no_placeholders_passthrough(self):
        store = self._make_store()
        text = "Just normal text."
        assert _resolve_placeholders(text, store) == text

    def test_missing_ref_comment(self):
        store = self._make_store()
        text = "<<wb:nonexistent/path>>"
        result = _resolve_placeholders(text, store)
        assert "<!-- wb: nonexistent/path not found -->" in result

    def test_recursive_resolves_chains(self):
        """<<wb:task-new --recursive>> should resolve task-new's own content
        including its inline <<wb:obsidian/bridge>> AND its context_after."""
        store = self._make_store()
        text = "<<wb:tasks/task-new --recursive>>"
        result = _resolve_placeholders(text, store)
        # task-new's own content should be there
        assert "Create a task." in result
        # task-new's inline placeholder (obsidian/bridge) should be resolved
        assert "Bridge failure protocol content." in result

    def test_non_recursive_does_not_resolve_inner_placeholders(self):
        """Without --recursive, inner placeholders stay unresolved."""
        store = self._make_store()
        text = "<<wb:tasks/task-new>>"
        result = _resolve_placeholders(text, store)
        assert "Create a task." in result
        # Inner placeholder should NOT be resolved (raw text)
        assert "<<wb:obsidian/bridge>>" in result

    def test_full_chain_handoff_to_bridge(self):
        """The motivating example: handoff → task-new → bridge."""
        store = self._make_store()
        handoff = store["tasks/handoff"]
        result = handoff.tier("full", store=store)
        content = result["content"]
        # Own content
        assert "Write handoff." in content
        assert "Confirm." in content
        # Recursive resolution should include task-new's content
        assert "Create a task." in content
        # And transitively, bridge content
        assert "Bridge failure protocol content." in content

    def test_multiple_placeholders_on_separate_lines(self):
        store = self._make_store()
        text = "<<wb:obsidian/bridge>>\n\n<<wb:other>>"
        result = _resolve_placeholders(text, store)
        assert "Bridge failure protocol" in result
        assert "No placeholders here." in result

    def test_malformed_placeholder_treated_as_path(self):
        store = self._make_store()
        text = "<<wb:obsidian/bridge --unknownflag>>"
        result = _resolve_placeholders(text, store)
        # Should still resolve — unknown flags ignored gracefully
        assert "Bridge failure protocol content." in result

    def test_empty_placeholder_left_as_is(self):
        store = self._make_store()
        text = "<<wb:>>"
        result = _resolve_placeholders(text, store)
        assert "<<wb:>>" in result

    def test_backward_compat_context_after_still_works(self):
        """Units with only context_after (no placeholders) should work unchanged."""
        bridge = DirectionsUnit(
            path="obsidian/bridge", name="Bridge", description="bridge",
            content={"full": "Bridge info."},
        )
        unit = DirectionsUnit(
            path="x", name="X", description="x",
            content={"full": "Main content."},
            context_after=["obsidian/bridge"],
        )
        store = {"obsidian/bridge": bridge, "x": unit}
        result = unit.tier("full", store=store)
        assert "Main content." in result["content"]
        assert "Bridge info." in result["content"]
        assert "--- context from: obsidian/bridge ---" in result["content"]
