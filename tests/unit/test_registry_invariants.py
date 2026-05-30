"""Phase D — CI-time invariants for the MCP registry.

Fast-failing assertions that run on every `pytest tests/unit/` pass.
Catches common drift modes as the `invokes` backfill (Phase C) rolls
out:

    1. Every `invokes` entry (on Capability or WorkflowStep) must name
       an existing registry entry. Typos, renames, and dangling
       references all surface here.
    2. Every workflow step with auto_run must have a callable that
       starts with `work_buddy.` (the conductor's import-security rule).
    3. WorkflowDefinition.requires must actually equal the computed
       union — guards against someone hand-editing this field.

The test exercises the REAL registry (no mocks). If you add a new
capability that genuinely should have no invocations, either:

    - Leave `invokes` unset (defaults to []). The invariants treat
      missing as empty — no audit marker required.
    - Leave it genuinely empty with `invokes=[]`. Same result.

If the test FAILS, the error message names the offending entry
directly — do not edit this test to make it pass unless the invariant
itself changed.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def registry():
    """Unfiltered registry — see test_morning_routine_deps.py for rationale.

    Cache-reset via ``_REGISTRY = None`` rather than the full
    ``invalidate_registry()`` to avoid purging module state (which
    makes isinstance checks fail against the reimported class).
    """
    from unittest.mock import patch
    from work_buddy.mcp_server import registry as reg_mod

    reg_mod._REGISTRY = None
    with patch("work_buddy.tools.is_tool_available", return_value=True):
        reg = reg_mod.get_registry()
    yield reg
    reg_mod._REGISTRY = None


# ---------------------------------------------------------------------------
# Invariant 1 — every `invokes` points at a real capability/workflow
# ---------------------------------------------------------------------------

def test_every_invokes_entry_resolves(registry):
    """Every name in Capability.invokes or WorkflowStep.invokes exists
    in the registry OR is currently filtered out because a tool it
    requires is unavailable.

    The registry filters capabilities whose `requires` tools failed
    their probes at build time — those capabilities are tracked in
    ``DISABLED_CAPABILITIES`` rather than being true typos. We accept
    either state: registered, OR disabled-but-known. Anything else is
    a real dangling reference.

    Rationale: test environments probe against the user's live system;
    an Obsidian bridge that's briefly down at test time shouldn't make
    every workflow step that invokes an obsidian-requiring capability
    look like a typo.
    """
    from work_buddy.mcp_server.registry import Capability, WorkflowDefinition
    from work_buddy.tools import DISABLED_CAPABILITIES

    # Names that appear in agent prose today but haven't been promoted
    # to real registry entries yet. Add sparingly.
    allowlist: set[str] = set()

    # Capabilities that are KNOWN to exist but were filtered out of
    # this particular build because their required tool wasn't
    # available. Not dangling — just dormant.
    known_names = set(registry.keys()) | set(DISABLED_CAPABILITIES.keys()) | allowlist

    errors: list[str] = []
    for name, entry in registry.items():
        if isinstance(entry, Capability):
            for invoked in entry.invokes:
                if invoked not in known_names:
                    errors.append(
                        f"Capability '{name}' invokes '{invoked}' which is not registered"
                    )
        elif isinstance(entry, WorkflowDefinition):
            for step in entry.steps:
                for invoked in step.invokes:
                    if invoked not in known_names:
                        errors.append(
                            f"Workflow '{name}' step '{step.id}' invokes "
                            f"'{invoked}' which is not registered"
                        )

    assert not errors, "Dangling invokes references:\n  " + "\n  ".join(errors)


# ---------------------------------------------------------------------------
# Invariant 2 — auto_run callables use work_buddy.* prefix
# ---------------------------------------------------------------------------

def test_auto_run_callables_scoped_to_work_buddy(registry):
    """Every step.auto_run.callable starts with 'work_buddy.'.

    The conductor enforces this at execution time (security: prevents
    the workflow store from naming arbitrary import paths). Failing
    here early in CI is kinder than failing at runtime.
    """
    from work_buddy.mcp_server.registry import WorkflowDefinition

    errors: list[str] = []
    for name, entry in registry.items():
        if not isinstance(entry, WorkflowDefinition):
            continue
        for step in entry.steps:
            if step.auto_run is None:
                continue
            path = step.auto_run.callable
            if not path.startswith("work_buddy."):
                errors.append(
                    f"Workflow '{name}' step '{step.id}': auto_run.callable "
                    f"'{path}' does not start with 'work_buddy.'"
                )
    assert not errors, "Bad auto_run.callable paths:\n  " + "\n  ".join(errors)


# ---------------------------------------------------------------------------
# Invariant 3 — computed WorkflowDefinition.requires matches the resolver
# ---------------------------------------------------------------------------

def test_workflow_requires_matches_one_hop_union(registry):
    """WorkflowDefinition.requires should equal the one-hop step.requires ∪
    step.invokes-resolved .requires union.

    Guards against hand-edits to a workflow unit that try to set
    workflow-level `requires` directly — the field is supposed to be
    computed, so any hand-authored value would be silently overwritten
    at the next registry rebuild.
    """
    from work_buddy.mcp_server.registry import (
        Capability,
        WorkflowDefinition,
        _compute_workflow_requires,
    )

    # Snapshot current workflow requires
    snapshots: dict[str, list[str]] = {
        name: list(entry.requires)
        for name, entry in registry.items()
        if isinstance(entry, WorkflowDefinition)
    }

    # Re-run the computation against a fresh copy — if the result
    # differs from what the registry currently holds, something
    # out-of-band has diverged.
    recomputed_registry = dict(registry)
    for name in list(recomputed_registry):
        entry = recomputed_registry[name]
        if isinstance(entry, WorkflowDefinition):
            # Build a shallow copy so we don't mutate shared state
            wf_copy = WorkflowDefinition(
                name=entry.name,
                description=entry.description,
                workflow_file=entry.workflow_file,
                execution=entry.execution,
                allow_override=entry.allow_override,
                steps=list(entry.steps),
                context=entry.context,
                slash_command=entry.slash_command,
                requires=[],
            )
            recomputed_registry[name] = wf_copy

    _compute_workflow_requires(recomputed_registry)

    errors: list[str] = []
    for name, before in snapshots.items():
        after = list(recomputed_registry[name].requires)
        if sorted(before) != sorted(after):
            errors.append(
                f"Workflow '{name}'.requires diverged:\n"
                f"    registry: {sorted(before)}\n"
                f"    recomputed: {sorted(after)}"
            )
    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Invariant 4 — morning-routine's invokes entries cover obsidian + google_calendar
# ---------------------------------------------------------------------------

def test_morning_routine_requires_includes_core_components(registry):
    """Defense against accidental removal of morning-routine's invokes lists.

    If someone edits a workflow unit and breaks the flagship backfill,
    this test surfaces it immediately rather than silently at runtime.
    """
    wf = registry.get("morning-routine")
    assert wf is not None, "morning-routine workflow must be in the registry"
    assert "obsidian" in wf.requires, (
        f"morning-routine.requires lost 'obsidian'. Current: {wf.requires}"
    )
    assert "google_calendar" in wf.requires, (
        f"morning-routine.requires lost 'google_calendar'. Current: {wf.requires}"
    )


# ---------------------------------------------------------------------------
# Invariant 5 (CP-A1) — _DISABLED_REGISTRY stays in sync with DISABLED_CAPABILITIES
# ---------------------------------------------------------------------------


class TestDisabledRegistryInvariants:
    """The full Capability stash and the (name -> missing tools) dict must
    stay in lockstep so :func:`work_buddy.recovery.recheck_disabled_capability`
    can restore a disabled capability without re-running ``_build_registry``.
    """

    def _build_with_unavailable(self, unavailable_tools: list[str]):
        """Force a registry rebuild where the named tools probe as unavailable."""
        from unittest.mock import patch
        from work_buddy.mcp_server import registry as reg_mod

        def fake_is_available(tool_id: str) -> bool:
            return tool_id not in unavailable_tools

        reg_mod._REGISTRY = None
        with patch("work_buddy.tools.is_tool_available", side_effect=fake_is_available):
            reg = reg_mod.get_registry()
        return reg, reg_mod

    def test_disabled_keys_match_when_obsidian_unavailable(self):
        """Forcing obsidian unavailable: every disabled cap appears in BOTH
        DISABLED_CAPABILITIES and _DISABLED_REGISTRY with matching keys."""
        from work_buddy.tools import DISABLED_CAPABILITIES

        try:
            _, reg_mod = self._build_with_unavailable(["obsidian"])
            assert set(reg_mod._DISABLED_REGISTRY.keys()) == set(DISABLED_CAPABILITIES.keys()), (
                "_DISABLED_REGISTRY keys diverged from DISABLED_CAPABILITIES"
            )
            # And the set should be non-empty for the test to be meaningful.
            assert DISABLED_CAPABILITIES, (
                "Test setup expected at least one obsidian-requiring capability "
                "to land in DISABLED_CAPABILITIES — none did. Did the registry "
                "change such that no capability requires obsidian?"
            )
        finally:
            reg_mod._REGISTRY = None

    def test_disabled_and_live_registries_disjoint(self):
        """A capability cannot simultaneously be in _REGISTRY and _DISABLED_REGISTRY."""
        try:
            reg, reg_mod = self._build_with_unavailable(["obsidian"])
            overlap = set(reg.keys()) & set(reg_mod._DISABLED_REGISTRY.keys())
            assert not overlap, (
                f"Capability(ies) appear in both _REGISTRY and _DISABLED_REGISTRY: {overlap}"
            )
        finally:
            reg_mod._REGISTRY = None

    def test_stash_holds_full_capability_objects(self):
        """The stash must contain the actual Capability instance (not just metadata)
        so recovery can restore it with its callable intact."""
        from work_buddy.mcp_server.registry import Capability

        try:
            _, reg_mod = self._build_with_unavailable(["obsidian"])
            for name, entry in reg_mod._DISABLED_REGISTRY.items():
                assert isinstance(entry, Capability), (
                    f"_DISABLED_REGISTRY[{name!r}] is {type(entry).__name__}, "
                    f"expected Capability"
                )
                assert callable(entry.callable), (
                    f"_DISABLED_REGISTRY[{name!r}].callable is not callable"
                )
        finally:
            reg_mod._REGISTRY = None

    def test_stash_cleared_on_rebuild_no_leak(self):
        """_DISABLED_REGISTRY must be cleared at the top of every _build_registry()
        invocation so a stale Capability whose closure references a purged
        module never survives a reload."""
        from work_buddy.tools import DISABLED_CAPABILITIES

        try:
            # Build 1: obsidian unavailable -> populate stash.
            _, reg_mod = self._build_with_unavailable(["obsidian"])
            stash_after_build_1 = dict(reg_mod._DISABLED_REGISTRY)
            assert stash_after_build_1, "Test setup needs at least one stash entry"

            # Build 2: everything available -> stash should be EMPTY.
            reg_mod._REGISTRY = None
            from unittest.mock import patch
            with patch("work_buddy.tools.is_tool_available", return_value=True):
                reg_mod.get_registry()

            assert not reg_mod._DISABLED_REGISTRY, (
                "_DISABLED_REGISTRY leaked stale entries across rebuild: "
                f"{list(reg_mod._DISABLED_REGISTRY.keys())}"
            )
            assert not DISABLED_CAPABILITIES, (
                "DISABLED_CAPABILITIES leaked across rebuild: "
                f"{list(DISABLED_CAPABILITIES.keys())}"
            )
        finally:
            reg_mod._REGISTRY = None

    def test_get_disabled_registry_returns_sync_view(self):
        """The public accessor ``get_disabled_registry()`` must return the
        same dict that the filter pass populates — not a copy or empty dict."""
        from work_buddy.mcp_server.registry import get_disabled_registry

        try:
            _, reg_mod = self._build_with_unavailable(["obsidian"])
            view = get_disabled_registry()
            assert view is reg_mod._DISABLED_REGISTRY, (
                "get_disabled_registry() returned a different dict than the "
                "module-level _DISABLED_REGISTRY — the recovery module needs "
                "the live one to mutate."
            )
        finally:
            reg_mod._REGISTRY = None


# ---------------------------------------------------------------------------
# _entry_to_dict — duck-typed shape discrimination
# ---------------------------------------------------------------------------
# Across mcp_registry_reload, the Capability / WorkflowDefinition class
# objects change identity (sys.modules is purged, classes are re-imported).
# Pre-reload entries cached anywhere outside the registry no longer match
# isinstance against the post-reload classes; the symptom is an
# AttributeError on ``.execution`` leaking through error-handling paths
# (gateway.py:1439, 1524) as ``'Capability' object has no attribute
# 'execution'``. _entry_to_dict discriminates on shape, not isinstance,
# to survive that stale-class case.


class TestEntryToDictDuckTyping:
    def test_capability_shape_serialized_correctly(self):
        """An object with .callable but no .steps must be serialized
        through the capability branch — even if isinstance fails (e.g.
        post-reload stale class identity)."""
        from work_buddy.mcp_server.registry import _entry_to_dict

        class FakeCapability:
            name = "fake.op"
            description = "test"
            category = "test"
            parameters = {}
            callable = staticmethod(lambda: None)
            mutates_state = False
            retry_policy = "manual"
            slash_command = None

        d = _entry_to_dict(FakeCapability())
        assert d["type"] == "function"
        assert d["name"] == "fake.op"
        # Did NOT fall into the workflow branch:
        assert "execution" not in d
        assert "steps" not in d

    def test_workflow_shape_serialized_correctly(self):
        """An object with .steps must be serialized through the workflow
        branch even if isinstance fails."""
        from work_buddy.mcp_server.registry import _entry_to_dict

        class FakeStep:
            id = "s1"
            name = "Step 1"
            step_type = "reasoning"
            execution = "main"
            depends_on = []
            workflow_file = None

        class FakeWorkflow:
            name = "fake-workflow"
            description = "test"
            execution = "main"
            steps = [FakeStep()]
            slash_command = None

        d = _entry_to_dict(FakeWorkflow())
        assert d["type"] == "workflow"
        assert d["execution"] == "main"
        assert len(d["steps"]) == 1
        assert d["steps"][0]["id"] == "s1"
