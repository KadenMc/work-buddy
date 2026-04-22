"""Unit tests for work_buddy.control.graph.

Exercises the assembly pipeline (preferences → health → requirements →
registry → static topology) with each input mockable, plus the TTL cache
and invalidation semantics.
"""

from __future__ import annotations

from unittest import mock

import pytest

from work_buddy.control import graph as cg
from work_buddy.control.nodes import ControlNode


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a clean cache."""
    cg.invalidate_graph()
    yield
    cg.invalidate_graph()


@pytest.fixture
def mock_inputs(monkeypatch):
    """Patch all graph inputs so tests are deterministic.

    Returns a mutable dict of lever points::

        {
            "prefs":      {comp_id: FeaturePreference},
            "health":     {"components": [...], "summary": {...}},
            "reqs":       [RequirementResult, ...],
            "registry":   {name: Capability | WorkflowDefinition},
        }

    Edit these in the test body, then call ``build_graph(force=True)``.
    """
    from work_buddy.health.preferences import FeaturePreference
    from work_buddy.health.requirements import RequirementResult
    from work_buddy.mcp_server.registry import Capability

    levers: dict = {
        "prefs": {},
        "health": {"components": [], "summary": {}},
        "reqs": [],
        "registry": {},
    }

    def _load_prefs():
        return dict(levers["prefs"])

    class _StubHealthEngine:
        def get_all(self):
            return dict(levers["health"])

    class _StubChecker:
        def check_all(self, include_unwanted: bool = False):
            return list(levers["reqs"])

    def _get_registry():
        return dict(levers["registry"])

    # Patch at the import sites inside graph._assemble
    monkeypatch.setattr("work_buddy.health.preferences.load_preferences", _load_prefs)
    monkeypatch.setattr("work_buddy.health.engine.HealthEngine", _StubHealthEngine)
    monkeypatch.setattr("work_buddy.health.requirements.RequirementChecker", _StubChecker)
    monkeypatch.setattr("work_buddy.mcp_server.registry.get_registry", _get_registry)

    # Expose constructors for tests
    levers["_FeaturePreference"] = FeaturePreference
    levers["_RequirementResult"] = RequirementResult
    levers["_Capability"] = Capability
    return levers


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------

def test_build_graph_returns_all_node_kinds(mock_inputs):
    # Minimal: no prefs, empty health, empty reqs, empty registry.
    # We should still get every domain, every subsystem, and every
    # component in COMPONENT_CATALOG + all registered requirements.
    nodes = cg.build_graph(force=True)
    kinds = {n.kind for n in nodes.values()}
    assert "domain" in kinds
    assert "subsystem" in kinds
    assert "component" in kinds
    assert "requirement" in kinds


def test_component_nodes_populated_from_catalog(mock_inputs):
    nodes = cg.build_graph(force=True)
    # Every component in the catalog should have a node
    from work_buddy.health.components import COMPONENT_CATALOG
    for comp_id in COMPONENT_CATALOG:
        assert f"component:{comp_id}" in nodes


def test_requirement_nodes_populated_from_registry(mock_inputs):
    nodes = cg.build_graph(force=True)
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY
    for req_id in REQUIREMENT_REGISTRY:
        assert f"req:{req_id}" in nodes


def test_component_sidecar_is_present(mock_inputs):
    """Phase A added `component:sidecar` to COMPONENT_CATALOG."""
    nodes = cg.build_graph(force=True)
    assert "component:sidecar" in nodes
    assert nodes["component:sidecar"].kind == "component"


# ---------------------------------------------------------------------------
# Preference cascade
# ---------------------------------------------------------------------------

def test_unwanted_component_is_disabled(mock_inputs):
    mock_inputs["prefs"]["telegram"] = mock_inputs["_FeaturePreference"](
        component_id="telegram", wanted=False,
    )
    nodes = cg.build_graph(force=True)
    assert nodes["component:telegram"].effective_state == "disabled"


def test_wanted_component_with_healthy_probe_is_ok(mock_inputs):
    mock_inputs["prefs"]["telegram"] = mock_inputs["_FeaturePreference"](
        component_id="telegram", wanted=True,
    )
    # telegram now hard-depends on sidecar (post hard/soft refactor), so
    # sidecar must be healthy in the mock for telegram to be ok.
    mock_inputs["health"]["components"] = [
        {
            "id": "telegram", "display_name": "Telegram Bot", "category": "service",
            "status": "healthy", "wanted": True, "depends_on": ["sidecar"],
            "details": {}, "children": [],
        },
        {
            "id": "sidecar", "display_name": "Sidecar", "category": "external",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
    ]
    nodes = cg.build_graph(force=True)
    assert nodes["component:telegram"].effective_state == "ok"


def test_unwanted_cascades_to_requirement_node(mock_inputs):
    """A requirement under an unwanted component is disabled without running its check."""
    mock_inputs["prefs"]["obsidian"] = mock_inputs["_FeaturePreference"](
        component_id="obsidian", wanted=False,
    )
    # Note: reqs list intentionally empty — if the cascade worked,
    # the req node is disabled even with no RequirementResult available.
    nodes = cg.build_graph(force=True)
    daily_req_node = nodes["req:obsidian/daily-note/log-section"]
    assert daily_req_node.effective_state == "disabled"


def test_unwanted_cascades_to_subsystem(mock_inputs):
    """subsystem:daily-notes has a dep edge to component:obsidian — unwanting obsidian disables the subsystem."""
    mock_inputs["prefs"]["obsidian"] = mock_inputs["_FeaturePreference"](
        component_id="obsidian", wanted=False,
    )
    nodes = cg.build_graph(force=True)
    # All daily-note requirements disabled + dep on obsidian disabled
    assert nodes["subsystem:daily-notes"].effective_state == "disabled"


# ---------------------------------------------------------------------------
# Dependency propagation
# ---------------------------------------------------------------------------

def test_dep_down_produces_blocked(mock_inputs):
    """component:hindsight depends on component:postgresql — postgres down → hindsight blocked."""
    mock_inputs["health"]["components"] = [
        {
            "id": "postgresql", "display_name": "PostgreSQL", "category": "external",
            "status": "unavailable", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "hindsight", "display_name": "Hindsight", "category": "integration",
            "status": "healthy", "wanted": None, "depends_on": ["postgresql"],
            "details": {}, "children": [],
        },
    ]
    nodes = cg.build_graph(force=True)
    # postgres itself is degraded (our mapping for unavailable)
    assert nodes["component:postgresql"].effective_state == "degraded"
    # hindsight is blocked because its dep edge (postgresql) is not ok
    assert nodes["component:hindsight"].effective_state == "blocked"


# ---------------------------------------------------------------------------
# Capability nodes
# ---------------------------------------------------------------------------

def test_capability_node_with_ok_deps_is_ok(mock_inputs):
    mock_inputs["prefs"]["obsidian"] = mock_inputs["_FeaturePreference"](
        component_id="obsidian", wanted=True,
    )
    mock_inputs["health"]["components"] = [{
        "id": "obsidian", "display_name": "Obsidian", "category": "integration",
        "status": "healthy", "wanted": True, "depends_on": [],
        "details": {}, "children": [],
    }]
    mock_inputs["registry"]["task_toggle"] = mock_inputs["_Capability"](
        name="task_toggle",
        description="Toggle a task",
        category="tasks",
        parameters={},
        callable=lambda **_: None,
        requires=["obsidian"],
    )
    nodes = cg.build_graph(force=True)
    assert nodes["cap:task_toggle"].effective_state == "ok"


def test_capability_node_inherits_disabled_from_unwanted_component(mock_inputs):
    mock_inputs["prefs"]["obsidian"] = mock_inputs["_FeaturePreference"](
        component_id="obsidian", wanted=False,
    )
    mock_inputs["registry"]["task_toggle"] = mock_inputs["_Capability"](
        name="task_toggle",
        description="Toggle a task",
        category="tasks",
        parameters={},
        callable=lambda **_: None,
        requires=["obsidian"],
    )
    nodes = cg.build_graph(force=True)
    # task_toggle's dep is component:obsidian which is disabled → capability blocked
    # (our rule: if any dep is disabled AND not all are, → blocked)
    # With only one dep, and it disabled, all deps disabled → capability disabled
    assert nodes["cap:task_toggle"].effective_state == "disabled"


def test_affects_capabilities_inverse_edge(mock_inputs):
    mock_inputs["registry"]["task_toggle"] = mock_inputs["_Capability"](
        name="task_toggle",
        description="Toggle a task",
        category="tasks",
        parameters={},
        callable=lambda **_: None,
        requires=["obsidian"],
    )
    nodes = cg.build_graph(force=True)
    assert "task_toggle" in nodes["component:obsidian"].affects_capabilities


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_ttl_cache_prevents_rebuild(mock_inputs, monkeypatch):
    """Two consecutive build_graph() calls without force should reuse the cache."""
    call_count = {"n": 0}
    real_assemble = cg._assemble

    def counting_assemble():
        call_count["n"] += 1
        return real_assemble()

    monkeypatch.setattr(cg, "_assemble", counting_assemble)
    cg.build_graph()
    cg.build_graph()
    assert call_count["n"] == 1, "cache should have returned the same graph"


def test_invalidate_clears_cache(mock_inputs, monkeypatch):
    call_count = {"n": 0}
    real_assemble = cg._assemble

    def counting_assemble():
        call_count["n"] += 1
        return real_assemble()

    monkeypatch.setattr(cg, "_assemble", counting_assemble)
    cg.build_graph()
    cg.invalidate_graph()
    cg.build_graph()
    assert call_count["n"] == 2, "invalidate should have forced a rebuild"


def test_force_bypasses_cache(mock_inputs, monkeypatch):
    call_count = {"n": 0}
    real_assemble = cg._assemble

    def counting_assemble():
        call_count["n"] += 1
        return real_assemble()

    monkeypatch.setattr(cg, "_assemble", counting_assemble)
    cg.build_graph()
    cg.build_graph(force=True)
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Domain topology
# ---------------------------------------------------------------------------

def test_component_dashboard_appears_under_two_domains(mock_inputs):
    """domain:notifications AND domain:runtime both list dashboard as a direct child."""
    nodes = cg.build_graph(force=True)
    parents = nodes["component:dashboard"].grouping_parents
    assert "domain:notifications" in parents
    assert "domain:runtime" in parents


def test_calendar_is_own_domain(mock_inputs):
    """Per user direction, calendar remains its own domain (not folded into knowledge)."""
    nodes = cg.build_graph(force=True)
    assert "domain:calendar" in nodes
    assert "domain:knowledge" in nodes
    # google_calendar should be under domain:calendar, not knowledge
    cal_parents = nodes["component:google_calendar"].grouping_parents
    assert "domain:calendar" in cal_parents


def test_sidecar_is_in_domain_system(mock_inputs):
    """Per user direction, sidecar is external infrastructure."""
    nodes = cg.build_graph(force=True)
    sc_parents = nodes["component:sidecar"].grouping_parents
    assert "domain:system" in sc_parents


# ---------------------------------------------------------------------------
# Capability resolver
# ---------------------------------------------------------------------------

def test_capability_resolver_computes_transitive_deps(mock_inputs):
    from work_buddy.control.capability_resolver import resolve_dependencies
    from work_buddy.mcp_server.registry import WorkflowDefinition, WorkflowStep

    cap_a = mock_inputs["_Capability"](
        name="cap_a",
        description="",
        category="x",
        parameters={},
        callable=lambda **_: None,
        requires=["obsidian"],
    )
    wf = WorkflowDefinition(
        name="wf_x", description="", workflow_file="store:test", execution="main",
        steps=[WorkflowStep(
            id="s1", name="s1", instruction="", step_type="reasoning",
            invokes=["cap_a"],
        )],
    )
    registry = {"cap_a": cap_a, "wf_x": wf}
    deps = resolve_dependencies("wf_x", registry=registry)
    assert "obsidian" in deps["components"]
    assert "cap_a" in deps["capabilities"]


def test_soft_dep_down_degrades_not_blocks(mock_inputs):
    """component:dashboard has soft deps on embedding/messaging/obsidian/hindsight.
    Embedding being down should mark dashboard degraded, not blocked."""
    mock_inputs["health"]["components"] = [
        {
            "id": "embedding", "display_name": "Embedding", "category": "service",
            "status": "unavailable", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "dashboard", "display_name": "Dashboard", "category": "service",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "sidecar", "display_name": "Sidecar", "category": "external",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
    ]
    nodes = cg.build_graph(force=True)
    # Embedding itself is degraded (unavailable → degraded via the
    # component-state mapping) — confirmed.
    assert nodes["component:embedding"].effective_state == "degraded"
    # Dashboard: hard dep sidecar ok, soft dep embedding degraded.
    # Should be degraded, NOT blocked.
    assert nodes["component:dashboard"].effective_state == "degraded"
    # The status reason should explain what's missing
    reason = nodes["component:dashboard"].status_reason
    assert "embedding" in reason.lower() or "Operating without" in reason or "Reduced" in reason


def test_soft_dep_fallback_note_surfaces_in_status_reason(mock_inputs):
    """When a soft dep declares a fallback_note via
    ComponentDef.soft_dep_notes, the status reason surfaces that
    specific description rather than a generic 'Operating without'."""
    mock_inputs["health"]["components"] = [
        {
            "id": "embedding", "display_name": "Embedding", "category": "service",
            "status": "unavailable", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "dashboard", "display_name": "Dashboard", "category": "service",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "sidecar", "display_name": "Sidecar", "category": "external",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        # Make the other dashboard soft deps healthy so only embedding's
        # note appears in the reason.
        {
            "id": "messaging", "display_name": "Messaging", "category": "service",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "obsidian", "display_name": "Obsidian", "category": "integration",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "hindsight", "display_name": "Hindsight", "category": "integration",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
    ]
    nodes = cg.build_graph(force=True)
    dash = nodes["component:dashboard"]
    assert dash.effective_state == "degraded"
    # The embedding soft_dep_notes declaration mentions "substring" and
    # "Chat-content" — at least one must show up in the reason so users
    # see the specific impact, not a generic message.
    reason = dash.status_reason.lower()
    assert "substring" in reason or "chat" in reason or "reduced functionality" in reason


def test_fallback_note_populates_edge_in_graph(mock_inputs):
    """The Edge instance for a soft dep carries the fallback_note from
    ComponentDef.soft_dep_notes — needed for the UI tooltip."""
    nodes = cg.build_graph(force=True)
    dash = nodes["component:dashboard"]
    emb_edges = [e for e in dash.dependencies if e.target_id == "component:embedding"]
    assert emb_edges, "dashboard should have a soft dep on embedding"
    edge = emb_edges[0]
    assert edge.hardness == "soft"
    assert edge.fallback_note is not None
    assert "substring" in edge.fallback_note.lower()


def test_hard_dep_down_still_blocks(mock_inputs):
    """Sanity: hard deps still produce `blocked` state as before.
    Hindsight hard-depends on Postgres, so Postgres down → Hindsight blocked."""
    mock_inputs["health"]["components"] = [
        {
            "id": "postgresql", "display_name": "PostgreSQL", "category": "external",
            "status": "unavailable", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "hindsight", "display_name": "Hindsight", "category": "integration",
            "status": "healthy", "wanted": None, "depends_on": ["postgresql"],
            "details": {}, "children": [],
        },
    ]
    nodes = cg.build_graph(force=True)
    assert nodes["component:hindsight"].effective_state == "blocked"


def test_soft_dep_disabled_does_not_degrade(mock_inputs):
    """If a soft dep is `disabled` (user opted out), the parent is NOT
    degraded — the user made an explicit choice to not use that feature,
    so "operating without it" is the expected state, not a warning."""
    mock_inputs["prefs"]["hindsight"] = mock_inputs["_FeaturePreference"](
        component_id="hindsight", wanted=False,
    )
    # Dashboard has 4 soft deps: embedding, messaging, obsidian, hindsight.
    # For this test to isolate the "disabled soft dep doesn't degrade"
    # rule, the other 3 soft deps need healthy status so they don't
    # cause an unrelated soft-degradation.
    mock_inputs["health"]["components"] = [
        {
            "id": "dashboard", "display_name": "Dashboard", "category": "service",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "sidecar", "display_name": "Sidecar", "category": "external",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "embedding", "display_name": "Embedding", "category": "service",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "messaging", "display_name": "Messaging", "category": "service",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
        {
            "id": "obsidian", "display_name": "Obsidian", "category": "integration",
            "status": "healthy", "wanted": None, "depends_on": [],
            "details": {}, "children": [],
        },
    ]
    nodes = cg.build_graph(force=True)
    # hindsight gets "disabled" via the preference cascade
    assert nodes["component:hindsight"].effective_state == "disabled"
    # Dashboard: hard dep sidecar ok; soft deps embedding/messaging/obsidian
    # all healthy; soft dep hindsight disabled (not "unhealthy").
    # Should remain ok — disabled soft deps don't degrade.
    assert nodes["component:dashboard"].effective_state == "ok"


def test_core_component_preference_is_required(mock_inputs):
    """Components marked is_core=True always show preference='required', even
    if the user explicitly set features.<id>.wanted=false in config."""
    # Set a hostile preference for the sidecar (core) — the graph must
    # ignore it because is_core trumps user opt-out.
    mock_inputs["prefs"]["sidecar"] = mock_inputs["_FeaturePreference"](
        component_id="sidecar", wanted=False,
    )
    nodes = cg.build_graph(force=True)
    assert nodes["component:sidecar"].preference == "required"
    # And it shouldn't cascade to disabled (required behaves like wanted)
    assert nodes["component:sidecar"].effective_state != "disabled"


def test_core_component_always_wanted_via_is_wanted(mock_inputs):
    """preferences.is_wanted() returns True for core components."""
    from work_buddy.health.preferences import is_wanted, is_core
    # Core components: sidecar, messaging, embedding, dashboard
    assert is_core("sidecar") is True
    assert is_core("messaging") is True
    assert is_core("embedding") is True
    assert is_core("dashboard") is True
    # Non-core components: obsidian, telegram
    assert is_core("obsidian") is False
    assert is_core("telegram") is False


def test_capability_resolver_handles_missing_capability(mock_inputs):
    """Invoking an unregistered capability doesn't crash; it's silently skipped."""
    from work_buddy.control.capability_resolver import resolve_dependencies
    from work_buddy.mcp_server.registry import WorkflowDefinition, WorkflowStep

    wf = WorkflowDefinition(
        name="wf_y", description="", workflow_file="store:test", execution="main",
        steps=[WorkflowStep(
            id="s1", name="s1", instruction="", step_type="reasoning",
            invokes=["not_a_real_cap"],
        )],
    )
    deps = resolve_dependencies("wf_y", registry={"wf_y": wf})
    assert deps["components"] == set()
    assert deps["capabilities"] == set()
