"""Transitive dependency resolution for capabilities and workflows.

Given a registry entry (atomic capability or workflow) by name, walk
the ``invokes`` edges and union the ``requires`` (tool/component IDs)
encountered along the way. Used by :mod:`work_buddy.control.graph` to
drive the ``affects_capabilities`` inverse edge and the
``effective_state`` of capability nodes.

The multi-hop closure lives here rather than on
``WorkflowDefinition.requires`` (which only does a one-hop union at
registry-build time) so that circular ``invokes`` chains and future
``mode="any"`` dep semantics can evolve without touching the registry
dataclass.
"""

from __future__ import annotations

from typing import Any


def resolve_dependencies(
    name: str,
    registry: dict[str, Any] | None = None,
) -> dict[str, set[str]]:
    """Return transitive dependencies of a capability or workflow.

    Returns a dict with three sets:

        components    Tool/component IDs this entry depends on
                      (equivalent to ``tools`` today; kept separate for
                       forward-compatibility with a future
                       ``ComponentProbe`` rename).
        capabilities  Capability names reached via ``invokes``.
        tools         Alias for ``components`` (tool_id ≡ component_id
                      by current convention).

    If ``registry`` is omitted, loads it via
    ``work_buddy.mcp_server.registry.get_registry`` (Phase A; future
    callers may pass a pre-built registry for testing).
    """
    if registry is None:
        from work_buddy.mcp_server.registry import get_registry
        registry = get_registry()

    from work_buddy.mcp_server.registry import Capability, WorkflowDefinition

    components: set[str] = set()
    capabilities: set[str] = set()

    visited: set[str] = set()
    stack: list[str] = [name]

    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)

        entry = registry.get(current)
        if entry is None:
            continue

        if isinstance(entry, Capability):
            capabilities.add(current)
            for t_id in entry.requires:
                components.add(t_id)
            for invoked in entry.invokes:
                if invoked not in visited:
                    stack.append(invoked)

        elif isinstance(entry, WorkflowDefinition):
            # Walk every step's requires + invokes
            for step in entry.steps:
                for t_id in step.requires:
                    components.add(t_id)
                for invoked in step.invokes:
                    if invoked not in visited:
                        stack.append(invoked)

    # Don't include the starting workflow/capability in the "capabilities"
    # set — it represents "who called us", not "what we transitively need."
    capabilities.discard(name)

    return {
        "components": components,
        "capabilities": capabilities,
        "tools": set(components),  # alias
    }


def capabilities_affected_by_component(
    component_id: str,
    registry: dict[str, Any] | None = None,
) -> list[str]:
    """Inverse edge: which registered capabilities / workflows depend on this component?

    A capability is "affected" if the component appears in its transitive
    dependency set (via ``requires`` directly or via an ``invokes`` chain).
    Used to populate :attr:`ControlNode.affects_capabilities` on component
    nodes.
    """
    if registry is None:
        from work_buddy.mcp_server.registry import get_registry
        registry = get_registry()

    from work_buddy.mcp_server.registry import Capability, WorkflowDefinition

    affected: list[str] = []
    for name, entry in registry.items():
        if not isinstance(entry, (Capability, WorkflowDefinition)):
            continue
        deps = resolve_dependencies(name, registry)
        if component_id in deps["components"]:
            affected.append(name)
    return sorted(affected)
