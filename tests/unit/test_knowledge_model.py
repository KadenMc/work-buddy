"""Unit tests for the KnowledgeUnit type hierarchy and context chaining."""

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
            path="personal/metacognition/branch-explosion",
            name="Branch Explosion",
            description="Too many branches",
            category="work_pattern",
            severity="HIGH",
            last_observed="2026-04-03",
            observation_count=12,
            source_file="work_patterns/branch-explosion.md",
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
