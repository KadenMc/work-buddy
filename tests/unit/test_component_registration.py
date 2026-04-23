"""Invariants the component / requirement / probe registrations must hold.

These are cheap static checks against the three parallel registries:

- ``work_buddy.health.components.COMPONENT_CATALOG``
- ``work_buddy.health.requirements.REQUIREMENT_REGISTRY``
- ``work_buddy.tools._TOOL_PROBES``

Registrations in one place silently affect behavior in the others. The
most painful example is the one this test was written to prevent: a
``ComponentDef(health_source="tool_probe")`` without a matching
``ToolProbe`` registration sits pinned at "unavailable" in the dashboard
Settings page and the user has no clue why. The engine doesn't warn —
it just reads ``{}`` from tool_status.json and shrugs.

Each test is a single invariant. When one fails, the message names the
specific component / requirement / probe that's out of sync so the
offender is trivial to locate.
"""

from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# ToolProbe ↔ ComponentDef consistency
# ---------------------------------------------------------------------------

def test_tool_probe_components_have_registered_probes() -> None:
    """Every ComponentDef declaring ``health_source="tool_probe"`` must
    have a matching entry in ``_TOOL_PROBES``.

    Without a registered probe, ``tool_status.json`` never gets an entry
    for this component id, and ``HealthEngine._merge_status`` returns
    "unavailable" indefinitely — the symptom that motivated this test.
    """
    from work_buddy.health.components import COMPONENT_CATALOG
    from work_buddy.tools import _register_default_probes, _TOOL_PROBES

    _register_default_probes()

    missing = [
        c.id for c in COMPONENT_CATALOG.values()
        if c.health_source == "tool_probe" and c.id not in _TOOL_PROBES
    ]
    assert not missing, (
        "Components declared health_source='tool_probe' without a "
        f"registered ToolProbe: {missing}. Register a ToolProbe in "
        "work_buddy/tools.py::_register_default_probes(), or change "
        "the component's health_source to 'custom' if this component "
        "is only evaluated on explicit diagnose."
    )


def test_composite_components_have_registered_probes() -> None:
    """``health_source="composite"`` merges a tool_probe with a sidecar
    service. The tool_probe half MUST exist — the sidecar half is
    optional (external services like ``hindsight`` declare composite
    but aren't sidecar-managed; the engine handles empty sidecar status
    as healthy when the probe is good). So we only enforce the probe.
    """
    from work_buddy.health.components import COMPONENT_CATALOG
    from work_buddy.tools import _register_default_probes, _TOOL_PROBES

    _register_default_probes()

    missing = [
        c.id for c in COMPONENT_CATALOG.values()
        if c.health_source == "composite" and c.id not in _TOOL_PROBES
    ]
    assert not missing, (
        "Composite components missing the tool-probe half: "
        f"{missing}. Register a ToolProbe or change health_source."
    )


# ---------------------------------------------------------------------------
# RequirementDef ↔ ComponentDef consistency
# ---------------------------------------------------------------------------

def test_requirement_components_exist() -> None:
    """Every RequirementDef.component (when non-None) must point at a
    real ComponentDef. A dangling component reference means the Settings
    page groups the requirement under a component that doesn't exist and
    nobody ever sees it.
    """
    from work_buddy.health.components import COMPONENT_CATALOG
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    dangling = [
        (req.id, req.component)
        for req in REQUIREMENT_REGISTRY.values()
        if req.component is not None
        and req.component not in COMPONENT_CATALOG
    ]
    assert not dangling, (
        f"Requirements point at non-existent components: {dangling}. "
        "Either register the component or set req.component=None for "
        "core/bootstrap requirements."
    )


def test_component_requirement_ids_exist() -> None:
    """Every requirement id listed in ``ComponentDef.requirements`` must
    be a real entry in ``REQUIREMENT_REGISTRY``. A typo here means the
    component silently drops a setup-time check on the Settings page.
    """
    from work_buddy.health.components import COMPONENT_CATALOG
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    bad: list[tuple[str, str]] = []
    for comp in COMPONENT_CATALOG.values():
        for rid in comp.requirements:
            if rid not in REQUIREMENT_REGISTRY:
                bad.append((comp.id, rid))
    assert not bad, (
        "ComponentDef.requirements referencing unregistered requirement "
        f"ids: {bad}. Each entry must match a RequirementDef.id in "
        "work_buddy/health/requirements.py."
    )


# ---------------------------------------------------------------------------
# Dependency graph integrity
# ---------------------------------------------------------------------------

def test_depends_on_targets_exist() -> None:
    """``ComponentDef.depends_on`` and ``.soft_depends_on`` must point at
    other registered components. A dangling dep breaks the control-graph
    builder and silently hides the dependency edge from Settings.
    """
    from work_buddy.health.components import COMPONENT_CATALOG

    dangling: list[tuple[str, str, str]] = []
    for comp in COMPONENT_CATALOG.values():
        for dep in comp.depends_on:
            if dep not in COMPONENT_CATALOG:
                dangling.append((comp.id, "depends_on", dep))
        for dep in comp.soft_depends_on:
            if dep not in COMPONENT_CATALOG:
                dangling.append((comp.id, "soft_depends_on", dep))
    assert not dangling, (
        f"Dependency edges pointing at non-existent components: "
        f"{dangling}. Either register the target or remove the edge."
    )


def test_soft_dep_notes_keys_subset_of_soft_depends_on() -> None:
    """``soft_dep_notes`` keys must all appear in ``soft_depends_on``.
    A stale note key (from renaming a dep) is the kind of drift that
    makes the Settings UI claim a fallback behavior for a dep that
    isn't actually declared.
    """
    from work_buddy.health.components import COMPONENT_CATALOG

    stale: list[tuple[str, str]] = []
    for comp in COMPONENT_CATALOG.values():
        soft_set = set(comp.soft_depends_on)
        for note_key in comp.soft_dep_notes:
            if note_key not in soft_set:
                stale.append((comp.id, note_key))
    assert not stale, (
        f"soft_dep_notes keys not in soft_depends_on: {stale}. Either "
        "add the dep to soft_depends_on or remove the stale note."
    )


# ---------------------------------------------------------------------------
# check_fn dotted paths resolve
# ---------------------------------------------------------------------------

def _resolves(dotted: str) -> bool:
    """Can this ``module.attr`` dotted path be imported + getattr'd?"""
    try:
        module_path, attr = dotted.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return callable(getattr(module, attr, None))
    except Exception:
        return False


def test_component_check_fn_paths_resolve() -> None:
    """Every ``CheckStep.check_fn`` must be importable and callable.
    A broken dotted path surfaces at Diagnose time as an opaque
    'Check raised an error' — much easier to catch up front.
    """
    from work_buddy.health.components import COMPONENT_CATALOG

    broken: list[tuple[str, str]] = []
    for comp in COMPONENT_CATALOG.values():
        for step in comp.check_sequence:
            if not _resolves(step.check_fn):
                broken.append((comp.id, step.check_fn))
    assert not broken, (
        f"ComponentDef check_fn paths that don't resolve: {broken}"
    )


def test_requirement_check_fn_paths_resolve() -> None:
    """Every ``RequirementDef.check_fn`` must be importable and callable."""
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    broken: list[tuple[str, str]] = []
    for req in REQUIREMENT_REGISTRY.values():
        if not _resolves(req.check_fn):
            broken.append((req.id, req.check_fn))
    assert not broken, (
        f"RequirementDef check_fn paths that don't resolve: {broken}"
    )


def test_requirement_fix_fn_paths_resolve() -> None:
    """Every ``RequirementDef.fix_fn`` (when set) must resolve. A broken
    fix_fn makes the Settings 'Fix' button blow up with a traceback
    instead of a sensible error.
    """
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    broken: list[tuple[str, str]] = []
    for req in REQUIREMENT_REGISTRY.values():
        if req.fix_fn and not _resolves(req.fix_fn):
            broken.append((req.id, req.fix_fn))
    assert not broken, (
        f"RequirementDef fix_fn paths that don't resolve: {broken}"
    )
