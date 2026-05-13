"""Unit tests for the KnowledgeUnit type hierarchy, context chaining, and placeholders."""

import pytest

from work_buddy.knowledge.model import (
    KnowledgeUnit,
    PromptUnit,
    DirectionsUnit,
    SystemUnit,
    ServiceUnit,
    IntegrationUnit,
    ReferenceUnit,
    ConceptUnit,
    CapabilityUnit,
    WorkflowUnit,
    VaultUnit,
    unit_from_dict,
    validate_dag,
    _resolve_placeholders,
    _extract_placeholder_refs,
    _RECURSIVE_ALL_SIZE_CAP,
    _TRUNCATION_MARKER,
    _DEPTH_LIMIT_MARKER_TEMPLATE,
    _BACK_REFERENCE_MARKER_TEMPLATE,
    _DEFAULT_MAX_DEPTH_FOR_ALL_MODE,
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
        # Empty defaults for the structural list fields
        assert u.parents == []
        assert u.children == []

    def test_scope_field(self):
        u = KnowledgeUnit(
            path="x", kind="system", name="X", description="x",
            scope="personal",
        )
        assert u.scope == "personal"

    def test_tier_index_includes_scope(self):
        u = KnowledgeUnit(
            path="x", kind="system", name="X", description="x",
        )
        t = u.tier("index")
        assert t["scope"] == "system"
        assert t["path"] == "x"

    # Context-chain serialization tests removed when ``context_before``
    # / ``context_after`` fields were retired. Inline placeholders are
    # the surviving cross-unit content-reuse mechanism — see
    # TestResolvePlaceholders and TestRecursiveMode for coverage.


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


class TestNewKinds:
    """The 9-kind taxonomy: service, integration, reference, concept (added);
    SystemUnit narrowed to a prose-first domain anchor."""

    def test_system_unit_has_no_kind_specific_fields(self):
        s = SystemUnit(path="tasks", name="Tasks", description="Tasks domain")
        assert s.kind == "system"
        assert s._kind_fields() == {}
        assert not hasattr(s, "ports")
        assert not hasattr(s, "entry_points")

    def test_service_unit_construction(self):
        s = ServiceUnit(
            path="services/dashboard",
            name="Dashboard",
            description="Web UI",
            ports=[5127],
            health_url="/health",
            entry_points=["work_buddy.dashboard.service:main"],
        )
        assert s.kind == "service"
        assert s.ports == [5127]
        assert s.health_url == "/health"
        assert s._kind_fields() == {
            "ports": [5127],
            "health_url": "/health",
            "entry_points": ["work_buddy.dashboard.service:main"],
        }

    def test_service_unit_omits_empty_fields_in_dict(self):
        s = ServiceUnit(path="x", name="X", description="x")
        assert s._kind_fields() == {}

    def test_integration_unit_construction(self):
        i = IntegrationUnit(
            path="obsidian/bridge",
            name="Obsidian bridge",
            description="Plugin bridge",
            external_system="Obsidian",
            bridge_module="work_buddy.obsidian.bridge",
            ports=[27125],
        )
        assert i.kind == "integration"
        assert i.external_system == "Obsidian"
        assert i.bridge_module == "work_buddy.obsidian.bridge"
        assert i.ports == [27125]

    def test_reference_unit_construction(self):
        r = ReferenceUnit(
            path="automation/contexts",
            name="Action contexts",
            description="resolve_who_can_act",
            entry_points=[
                "work_buddy.automation.contexts.resolve_who_can_act",
                "work_buddy.automation.contexts.CONTEXT_REGISTRY",
            ],
        )
        assert r.kind == "reference"
        assert len(r.entry_points) == 2
        assert r._kind_fields() == {
            "entry_points": [
                "work_buddy.automation.contexts.resolve_who_can_act",
                "work_buddy.automation.contexts.CONTEXT_REGISTRY",
            ]
        }

    def test_concept_unit_has_no_kind_specific_fields(self):
        c = ConceptUnit(
            path="architecture/repo-structure",
            name="Repo structure",
            description="Layout",
        )
        assert c.kind == "concept"
        assert c._kind_fields() == {}

    def test_all_new_kinds_are_prompt_units(self):
        for cls in (SystemUnit, ServiceUnit, IntegrationUnit, ReferenceUnit, ConceptUnit):
            assert issubclass(cls, PromptUnit)


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

    def test_unit_from_dict_silently_drops_retired_chain_keys(self):
        """Stale ``context_before`` / ``context_after`` keys in legacy
        store JSON should be ignored, not raise — the loader tolerates
        them so we don't have to back-fill every file at once."""
        data = {
            "kind": "directions",
            "name": "Retro",
            "description": "Retro directions",
            "context_before": ["dev/dev-mode"],
            "context_after": ["dev/extra"],
        }
        u = unit_from_dict("dev/retro", data)
        assert isinstance(u, DirectionsUnit)
        # Fields no longer exist on the dataclass; presence as JSON keys
        # is silently ignored.
        assert not hasattr(u, "context_before")
        assert not hasattr(u, "context_after")

    def test_unit_from_dict_service(self):
        data = {
            "kind": "service",
            "name": "Dashboard",
            "description": "Web UI",
            "ports": [5127],
            "health_url": "/health",
            "entry_points": ["work_buddy.dashboard.service:main"],
        }
        u = unit_from_dict("services/dashboard", data)
        assert isinstance(u, ServiceUnit)
        assert u.ports == [5127]
        assert u.health_url == "/health"
        assert u.entry_points == ["work_buddy.dashboard.service:main"]

    def test_unit_from_dict_integration(self):
        data = {
            "kind": "integration",
            "name": "Obsidian bridge",
            "description": "Plugin bridge",
            "external_system": "Obsidian",
            "bridge_module": "work_buddy.obsidian.bridge",
            "ports": [27125],
        }
        u = unit_from_dict("obsidian/bridge", data)
        assert isinstance(u, IntegrationUnit)
        assert u.external_system == "Obsidian"
        assert u.bridge_module == "work_buddy.obsidian.bridge"
        assert u.ports == [27125]

    def test_unit_from_dict_reference(self):
        data = {
            "kind": "reference",
            "name": "Action contexts",
            "description": "API surface",
            "entry_points": ["work_buddy.automation.contexts.resolve_who_can_act"],
        }
        u = unit_from_dict("automation/contexts", data)
        assert isinstance(u, ReferenceUnit)
        assert u.entry_points == ["work_buddy.automation.contexts.resolve_who_can_act"]

    def test_unit_from_dict_concept(self):
        data = {
            "kind": "concept",
            "name": "Repo structure",
            "description": "Layout",
        }
        u = unit_from_dict("architecture/repo-structure", data)
        assert isinstance(u, ConceptUnit)
        assert u.kind == "concept"

    def test_unit_from_dict_round_trips(self):
        """Each new kind survives deserialize → to_dict round trip."""
        cases = [
            {"kind": "service", "name": "S", "description": "s",
             "ports": [5125], "health_url": "/h", "entry_points": ["m:f"]},
            {"kind": "integration", "name": "I", "description": "i",
             "external_system": "X", "bridge_module": "m"},
            {"kind": "reference", "name": "R", "description": "r",
             "entry_points": ["m:f"]},
            {"kind": "concept", "name": "C", "description": "c"},
            {"kind": "system", "name": "Y", "description": "y"},
        ]
        for data in cases:
            u = unit_from_dict("test/path", data)
            d = u.to_dict()
            for k, v in data.items():
                assert d[k] == v, f"round-trip lost {k} for kind={data['kind']}"

    def test_unknown_kind_falls_back_and_warns(self, caplog):
        """An unknown kind deserializes as bare PromptUnit and emits a warning,
        so future ad-hoc kinds surface visibly in load_store logs rather than
        silently breaking docs_gen renderers."""
        import logging
        data = {"kind": "made-up-kind", "name": "X", "description": "x"}
        with caplog.at_level(logging.WARNING, logger="work_buddy.knowledge.model"):
            u = unit_from_dict("ad/hoc", data)
        assert type(u) is PromptUnit
        assert u.kind == "made-up-kind"
        assert any("made-up-kind" in r.message for r in caplog.records)
        assert any("ad/hoc" in r.message for r in caplog.records)


class TestValidateDag:
    # Chain-validation tests removed when ``context_before`` /
    # ``context_after`` were retired. The DAG validator still tracks
    # parent/child edges and inline ``<<wb:>>`` placeholder edges; the
    # below tests cover both.

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
        """``<<wb:task-new --recursive>>`` should resolve task-new's own
        content including its inline ``<<wb:obsidian/bridge>>`` placeholder."""
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

    # ``test_backward_compat_context_after_still_works`` removed when
    # the chain mechanism was retired. The chain assertion in
    # ``test_full_chain_handoff_to_bridge`` (via task-new's
    # context_after) also went away — placeholder cascade still
    # delivers the bridge content through the --recursive path.


class TestRecursiveMode:
    """Caller-side override of per-placeholder ``--recursive`` flags.

    See ``_RECURSIVE_MODES`` in ``model.py`` for the contract:
    ``"default"`` honours the author's flag per placeholder; ``"all"``
    forces every placeholder to expand transitively (bounded by a size
    cap); ``"none"`` preserves placeholders literally.
    """

    def _chain_store(self):
        """A → plain ``<<wb:B>>`` → plain ``<<wb:C>>``. No --recursive flags."""
        c = DirectionsUnit(
            path="c", name="C", description="leaf",
            content={"full": "C-body."},
        )
        b = DirectionsUnit(
            path="b", name="B", description="mid",
            content={"full": "B-body. <<wb:c>>"},
        )
        a = DirectionsUnit(
            path="a", name="A", description="top",
            content={"full": "A-body. <<wb:b>>"},
        )
        return {"a": a, "b": b, "c": c}

    def test_default_mode_matches_legacy(self):
        """``recursive_mode='default'`` (and no arg at all) must produce
        byte-identical output to the historical no-mode call."""
        store = self._chain_store()
        text = "<<wb:a>>"
        legacy = _resolve_placeholders(text, store)
        explicit = _resolve_placeholders(text, store, recursive_mode="default")
        assert legacy == explicit

    def test_default_mode_leaves_inner_unexpanded(self):
        """Plain placeholder A→B inserts A's body RAW — B is never reached.

        This is the foot-gun the editor hint exists to catch: A's content
        contains ``<<wb:b>>`` as a literal substring, but B's body and
        B's downstream chain never appear in the resolved output.
        """
        store = self._chain_store()
        result = _resolve_placeholders("<<wb:a>>", store, recursive_mode="default")
        # A's raw body appears
        assert "A-body." in result
        # B's literal placeholder survives in A's raw body
        assert "<<wb:b>>" in result
        # And neither B's body nor C's body appears — only one level deep
        assert "B-body." not in result
        assert "C-body." not in result

    def test_all_mode_forces_transitive(self):
        """``recursive_mode='all'`` cascades through plain placeholders."""
        store = self._chain_store()
        result = _resolve_placeholders("<<wb:a>>", store, recursive_mode="all")
        assert "A-body." in result
        assert "B-body." in result
        assert "C-body." in result
        # And the inner literal is gone — it got resolved
        assert "<<wb:c>>" not in result

    def test_none_mode_preserves_literal(self):
        """``recursive_mode='none'`` returns placeholder markup unchanged."""
        store = self._chain_store()
        text = "Before <<wb:a>> and <<wb:b>> after."
        result = _resolve_placeholders(text, store, recursive_mode="none")
        # No expansion at all — original text returned
        assert result == text

    def test_none_mode_passthrough_with_no_store(self):
        """``recursive_mode='none'`` does not touch the store, so the
        early-return path is the same as 'no placeholders to resolve'."""
        store: dict = {}
        text = "<<wb:does-not-exist>>"
        result = _resolve_placeholders(text, store, recursive_mode="none")
        assert result == text

    def test_tier_full_threads_recursive_mode(self):
        """End-to-end via ``tier(depth='full')`` — the user-facing surface."""
        store = self._chain_store()

        default_out = store["a"].tier("full", store=store)["content"]
        all_out = store["a"].tier("full", store=store, recursive_mode="all")["content"]
        none_out = store["a"].tier("full", store=store, recursive_mode="none")["content"]

        assert "C-body." not in default_out
        assert "C-body." in all_out
        assert "<<wb:b>>" in none_out
        assert "B-body." not in none_out

    def test_all_mode_truncates_at_size_cap(self):
        """A pathological chain whose ``all`` expansion exceeds the size
        cap should terminate cleanly with the truncation marker.

        Passes ``max_depth=None`` to disable the depth cap so the size
        cap is exercised in isolation. Otherwise the depth cap (default
        10 in ``all`` mode) would fire first.
        """
        # Build a chain of N units each whose body is ~20KB. With cap
        # at 100KB and ~9 levels actually expanded under depth=None,
        # we'd hit ~180KB without the size cap. The cap should kick in.
        n_levels = 12
        body_chunk = "x" * 20000  # 20KB per level
        store: dict[str, DirectionsUnit] = {}
        for i in range(n_levels):
            next_ref = f"<<wb:u{i + 1}>>" if i + 1 < n_levels else ""
            store[f"u{i}"] = DirectionsUnit(
                path=f"u{i}", name=f"U{i}", description=f"level {i}",
                content={"full": f"{body_chunk} {next_ref}"},
            )

        # max_depth=None overrides the all-mode default to unlimited
        # so size cap is the only constraint.
        result = _resolve_placeholders(
            "<<wb:u0>>", store,
            recursive_mode="all", max_depth=None,
        )

        # Truncation marker must appear — proves the size cap fired.
        assert _TRUNCATION_MARKER in result, (
            "expected size-cap truncation marker once expansion exceeds the cap"
        )
        # And the output is bounded — should not be the full
        # n_levels * 20KB = 240KB. Allow a generous safety margin since
        # the budget is checked between replacements, not mid-string.
        assert len(result) < _RECURSIVE_ALL_SIZE_CAP * 3, (
            f"output length {len(result)} exceeds tripled cap "
            f"{_RECURSIVE_ALL_SIZE_CAP * 3}"
        )

    def test_all_mode_under_cap_completes_normally(self):
        """Chains that fit under the cap should NOT show the marker."""
        # Tiny 3-level chain — way under the 100KB cap.
        store = self._chain_store()
        result = _resolve_placeholders("<<wb:a>>", store, recursive_mode="all")
        assert _TRUNCATION_MARKER not in result

    def test_missing_ref_comment_in_all_mode(self):
        """Missing-ref behaviour fires when (and only when) we actually
        descend into the unit that holds the broken reference.

        In default mode we only resolve one level — the outer
        ``<<wb:a>>`` is replaced with A's raw body, which contains the
        literal ``<<wb:missing>>`` substring (never resolved). The
        missing-ref comment doesn't appear because no resolver call ever
        looks up ``missing``.

        In ``all`` mode we descend into A, the resolver walks A's body,
        and ``<<wb:missing>>`` fails the lookup with the standard
        not-found comment.
        """
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:missing>>"},
        )
        store = {"a": a}
        out_default = _resolve_placeholders("<<wb:a>>", store, recursive_mode="default")
        out_all = _resolve_placeholders("<<wb:a>>", store, recursive_mode="all")
        # Default mode: A's body inserted raw, including the literal markup
        assert "<<wb:missing>>" in out_default
        assert "<!-- wb: missing not found -->" not in out_default
        # All mode: descent into A triggers the lookup, comment appears
        assert "<!-- wb: missing not found -->" in out_all

    def test_invalid_mode_raises_in_agent_docs(self):
        """The capability surface validates ``recursive`` and returns an
        error dict (not a raise) so MCP transport stays well-typed."""
        from work_buddy.knowledge.query import agent_docs
        result = agent_docs(path="nonexistent/unit", recursive="bogus")
        assert "error" in result
        assert "bogus" in result["error"].lower() or "invalid" in result["error"].lower()


class TestMaxDepth:
    """Configurable depth limit. ``None`` / ``-1`` = mode default
    (unlimited in ``default`` mode, 10 in ``all`` mode). ``0`` = no
    recursion at all. Positive ints = exact depth cap.
    """

    def _deep_chain_store(self, n: int = 15):
        """Linear chain of n units, each plain-referencing the next."""
        store: dict[str, DirectionsUnit] = {}
        for i in range(n):
            next_ref = f"<<wb:u{i + 1}>>" if i + 1 < n else ""
            store[f"u{i}"] = DirectionsUnit(
                path=f"u{i}", name=f"U{i}", description=f"L{i}",
                content={"full": f"U{i}-body {next_ref}".rstrip()},
            )
        return store

    def test_all_mode_default_depth_cap_fires_at_10(self):
        """Without an explicit ``max_depth``, ``all`` mode caps at the
        default of ``_DEFAULT_MAX_DEPTH_FOR_ALL_MODE``."""
        assert _DEFAULT_MAX_DEPTH_FOR_ALL_MODE == 10
        store = self._deep_chain_store(15)
        result = _resolve_placeholders("<<wb:u0>>", store, recursive_mode="all")
        marker = _DEPTH_LIMIT_MARKER_TEMPLATE.format(depth=10)
        assert marker in result, (
            f"expected depth-limit marker '{marker}' in result"
        )
        # First few levels DID expand; the deep ones did not.
        assert "U0-body" in result
        assert "U9-body" in result
        assert "U14-body" not in result

    def test_explicit_max_depth_overrides_mode_default(self):
        """Caller-passed ``max_depth=3`` overrides the all-mode default."""
        store = self._deep_chain_store(10)
        result = _resolve_placeholders(
            "<<wb:u0>>", store,
            recursive_mode="all", max_depth=3,
        )
        marker = _DEPTH_LIMIT_MARKER_TEMPLATE.format(depth=3)
        assert marker in result
        assert "U0-body" in result
        assert "U2-body" in result
        assert "U5-body" not in result

    def test_max_depth_zero_disables_recursion(self):
        """``max_depth=0`` is the caller-side way to say 'no recursion'
        — equivalent to ``recursive='none'`` in effect, but expressed
        through the depth knob."""
        store = self._deep_chain_store(5)
        result = _resolve_placeholders(
            "<<wb:u0>>", store,
            recursive_mode="all", max_depth=0,
        )
        # The first placeholder pass fires at depth 0 — depth marker
        # appears in place of any expansion.
        marker = _DEPTH_LIMIT_MARKER_TEMPLATE.format(depth=0)
        assert marker in result
        assert "U0-body" not in result

    def test_default_mode_no_depth_cap_by_default(self):
        """``default`` mode preserves historical behaviour: no depth
        cap unless caller passes one. A long chain of explicit
        ``--recursive`` flags expands fully."""
        n = 15
        store: dict[str, DirectionsUnit] = {}
        for i in range(n):
            # Each level uses --recursive so default mode does cascade
            next_ref = f"<<wb:u{i + 1} --recursive>>" if i + 1 < n else ""
            store[f"u{i}"] = DirectionsUnit(
                path=f"u{i}", name=f"U{i}", description=f"L{i}",
                content={"full": f"U{i}-body {next_ref}".rstrip()},
            )
        result = _resolve_placeholders(
            "<<wb:u0 --recursive>>", store, recursive_mode="default",
        )
        # All levels expanded — historical default mode is unbounded
        assert "U0-body" in result
        assert "U14-body" in result
        depth_marker_substr = "expansion truncated at depth"
        assert depth_marker_substr not in result

    def test_default_mode_caller_can_opt_into_depth_cap(self):
        """Default mode honours an explicit ``max_depth`` even though
        the mode default is unlimited."""
        n = 10
        store: dict[str, DirectionsUnit] = {}
        for i in range(n):
            next_ref = f"<<wb:u{i + 1} --recursive>>" if i + 1 < n else ""
            store[f"u{i}"] = DirectionsUnit(
                path=f"u{i}", name=f"U{i}", description=f"L{i}",
                content={"full": f"U{i}-body {next_ref}".rstrip()},
            )
        result = _resolve_placeholders(
            "<<wb:u0 --recursive>>", store,
            recursive_mode="default", max_depth=4,
        )
        assert _DEPTH_LIMIT_MARKER_TEMPLATE.format(depth=4) in result
        assert "U0-body" in result
        assert "U3-body" in result
        assert "U7-body" not in result


class TestPerUnitOccurrenceCap:
    """Per-unit-occurrence cap (always on). The first appearance of a
    given target in a top-level expansion gets the content; subsequent
    references get a back-reference marker. Catches diamond graphs.
    """

    def test_diamond_graph_second_reference_is_back_ref(self):
        """top → A → foundation, top → B → foundation. Foundation
        should appear once; the second branch's reference should
        produce a back-ref marker."""
        foundation = DirectionsUnit(
            path="foundation", name="Foundation", description="leaf",
            content={"full": "FOUNDATION-BODY"},
        )
        branch_a = DirectionsUnit(
            path="branch_a", name="BranchA", description="a",
            content={"full": "A-body <<wb:foundation>>"},
        )
        branch_b = DirectionsUnit(
            path="branch_b", name="BranchB", description="b",
            content={"full": "B-body <<wb:foundation>>"},
        )
        top = DirectionsUnit(
            path="top", name="Top", description="t",
            content={"full": "top: <<wb:branch_a>> and <<wb:branch_b>>"},
        )
        store = {u.path: u for u in [foundation, branch_a, branch_b, top]}

        result = _resolve_placeholders(
            "<<wb:top>>", store, recursive_mode="all",
        )

        # Foundation content appears exactly once
        assert result.count("FOUNDATION-BODY") == 1
        # The other reference is a back-ref marker
        back_ref = _BACK_REFERENCE_MARKER_TEMPLATE.format(path="foundation")
        assert back_ref in result

    def test_repeated_plain_placeholder_within_same_unit(self):
        """Even within a single unit's content, if ``<<wb:X>>`` appears
        twice in ``all`` mode, the second occurrence gets the back-ref
        marker (catches authoring duplicates at runtime)."""
        x = DirectionsUnit(
            path="x", name="X", description="x",
            content={"full": "X-BODY"},
        )
        host = DirectionsUnit(
            path="host", name="Host", description="h",
            content={"full": "<<wb:x>> middle <<wb:x>>"},
        )
        store = {"x": x, "host": host}
        result = _resolve_placeholders(
            "<<wb:host>>", store, recursive_mode="all",
        )
        assert result.count("X-BODY") == 1
        assert _BACK_REFERENCE_MARKER_TEMPLATE.format(path="x") in result

    def test_default_mode_still_dedupes_diamond(self):
        """Per-unit-occurrence cap fires in ``default`` mode too — a
        diamond using explicit ``--recursive`` flags should still
        dedupe."""
        foundation = DirectionsUnit(
            path="foundation", name="Foundation", description="leaf",
            content={"full": "FOUNDATION-BODY"},
        )
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "A-body <<wb:foundation --recursive>>"},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "B-body <<wb:foundation --recursive>>"},
        )
        top = DirectionsUnit(
            path="top", name="Top", description="t",
            content={"full": "<<wb:a --recursive>> <<wb:b --recursive>>"},
        )
        store = {u.path: u for u in [foundation, a, b, top]}
        result = _resolve_placeholders(
            "<<wb:top --recursive>>", store, recursive_mode="default",
        )
        assert result.count("FOUNDATION-BODY") == 1
        assert _BACK_REFERENCE_MARKER_TEMPLATE.format(path="foundation") in result

    def test_self_reference_via_resolve_full_content_uses_back_ref(self):
        """When ``_resolve_full_content`` is the top-level entry, the
        unit being resolved is pre-added to ``_seen`` — so if it
        appears downstream, the back-ref marker fires (defence beyond
        validate_dag's cycle check)."""
        # A → B → A (would be a cycle, normally blocked at load time —
        # but the runtime guard should also catch it).
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "A-body <<wb:b>>"},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "B-body <<wb:a>>"},
        )
        store = {"a": a, "b": b}
        # Call _resolve_full_content directly — this primes _seen with
        # the unit being resolved.
        result = a._resolve_full_content(store, recursive_mode="all")
        assert "A-body" in result
        assert "B-body" in result
        # And the self-ref to A from B's body becomes a back-ref
        assert _BACK_REFERENCE_MARKER_TEMPLATE.format(path="a") in result
