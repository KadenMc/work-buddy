"""Control-graph builder — assembles :class:`ControlNode` graphs from live state.

Reads from:

    - ``work_buddy.health.preferences.load_preferences``
    - ``work_buddy.health.engine.HealthEngine.get_all``
    - ``work_buddy.health.requirements.RequirementChecker.check_all``
    - ``work_buddy.health.components.COMPONENT_CATALOG``
    - ``work_buddy.health.requirements.REQUIREMENT_REGISTRY``
    - ``work_buddy.mcp_server.registry.get_registry``
    - ``work_buddy.control.graph_static``

Writes to: nothing. Every build is side-effect-free.

Thread-safe caching via a module-level lock + 45-s TTL. Mutating calls
elsewhere (e.g. ``set_preference``) call :func:`invalidate_graph` to
clear the cache eagerly.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from work_buddy.control.nodes import (
    ControlNode,
    Edge,
    EffectiveState,
    NodeCache,
    Preference,
)

log = logging.getLogger(__name__)

_GRAPH_TTL_SECONDS = 45.0
_cache: NodeCache | None = None
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_graph(force: bool = False) -> dict[str, ControlNode]:
    """Return the current control graph.

    ``force=True`` bypasses the TTL cache and rebuilds from scratch.
    """
    global _cache
    with _cache_lock:
        now = time.time()
        if (
            not force
            and _cache is not None
            and (now - _cache.built_at) < _GRAPH_TTL_SECONDS
        ):
            return _cache.nodes
        nodes = _assemble()
        _cache = NodeCache(nodes=nodes, built_at=now)
        return nodes


def invalidate_graph() -> None:
    """Clear the cache so the next ``build_graph`` rebuilds from scratch."""
    global _cache
    with _cache_lock:
        _cache = None


def cache_info() -> dict[str, Any]:
    """Diagnostic: return cache state without forcing a rebuild."""
    with _cache_lock:
        if _cache is None:
            return {"cached": False}
        return {
            "cached": True,
            "built_at": _cache.built_at,
            "age_seconds": round(time.time() - _cache.built_at, 2),
            "node_count": len(_cache.nodes),
        }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _assemble() -> dict[str, ControlNode]:
    """Build the full graph from scratch. Called under lock by ``build_graph``."""
    from work_buddy.health.components import COMPONENT_CATALOG
    from work_buddy.health.engine import HealthEngine
    from work_buddy.health.preferences import load_preferences
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY, RequirementChecker
    from work_buddy.mcp_server.registry import (
        Capability,
        WorkflowDefinition,
        get_registry,
    )
    from work_buddy.control.graph_static import iter_static_nodes
    from work_buddy.control.capability_resolver import (
        resolve_dependencies,
    )

    prefs = load_preferences()

    # Health — full probe/sidecar view (unwanted components already
    # marked disabled by HealthEngine; see work_buddy/health/engine.py:190).
    health_view = HealthEngine().get_all()
    health_by_id: dict[str, dict[str, Any]] = {
        c["id"]: c for c in health_view.get("components", [])
    }

    # Requirements — include unwanted so the graph can mark them disabled
    # explicitly rather than having them vanish.
    checker = RequirementChecker()
    req_results = checker.check_all(include_unwanted=True)
    req_by_id: dict[str, Any] = {r.id: r for r in req_results}

    try:
        registry = get_registry()
    except Exception as exc:
        log.warning("Control graph: registry unavailable, capability nodes will be empty (%s)", exc)
        registry = {}

    nodes: dict[str, ControlNode] = {}

    # -----------------------------------------------------------------
    # Step 1 — domains and subsystems (from static topology)
    # -----------------------------------------------------------------
    for static in iter_static_nodes():
        node_id = static["id"]
        kind = "domain" if node_id.startswith("domain:") else "subsystem"

        deps: list[Edge] = []
        for dep_comp in static.get("component_deps", []):
            deps.append(Edge(target_id=f"component:{dep_comp}"))
        for dep_sub in static.get("subsystem_deps", []):
            deps.append(Edge(target_id=dep_sub))

        requirement_ids = [
            f"req:{rid}" for rid in static.get("requirement_ids", [])
        ]

        nodes[node_id] = ControlNode(
            id=node_id,
            kind=kind,  # type: ignore[arg-type]
            label=static.get("label", node_id),
            description=static.get("description", ""),
            grouping_parents=list(static.get("grouping_parents", [])),
            dependencies=deps,
            requirement_ids=requirement_ids,
        )

    # -----------------------------------------------------------------
    # Step 2 — components (from COMPONENT_CATALOG, augmented with grouping
    # from static topology's `children_components`)
    # -----------------------------------------------------------------
    # Reverse index: component_id → list of parent node ids that claim it
    # as a direct child (domains in children_components, subsystems via
    # component_deps).
    component_parents: dict[str, list[str]] = {}
    for static in iter_static_nodes():
        parent = static["id"]
        for cid in static.get("children_components", []):
            component_parents.setdefault(cid, []).append(parent)
        for cid in static.get("component_deps", []):
            # subsystems' `component_deps` imply the subsystem is the
            # grouping parent of those components too (so Obsidian
            # appears under subsystem:daily-notes visually).
            component_parents.setdefault(cid, []).append(parent)

    for comp_id, comp in COMPONENT_CATALOG.items():
        node_id = f"component:{comp_id}"
        health = health_by_id.get(comp_id, {})
        pref_obj = prefs.get(comp_id)
        preference = _preference_from_obj(pref_obj, is_core=comp.is_core)

        # Dependency edges — both hard and soft.
        # ``depends_on`` (hard, default): failure cascades as `blocked`.
        # ``soft_depends_on``: failure cascades as `degraded` at worst.
        dep_edges = [
            Edge(target_id=f"component:{dep_id}", hardness="hard")
            for dep_id in comp.depends_on
        ]
        dep_edges += [
            Edge(target_id=f"component:{dep_id}", hardness="soft")
            for dep_id in getattr(comp, "soft_depends_on", [])
        ]

        # Requirement ids for this component
        requirement_ids_for_comp = [
            f"req:{rid}" for rid in comp.requirements
        ]

        # Affects-capabilities inverse edge — computed lazily below

        nodes[node_id] = ControlNode(
            id=node_id,
            kind="component",
            label=comp.display_name,
            description=f"{comp.category.capitalize()} component.",
            grouping_parents=list(dict.fromkeys(component_parents.get(comp_id, []))),
            dependencies=dep_edges,
            preference=preference,
            effective_state="unknown",  # filled in step 5
            component_id=comp_id,
            requirement_ids=requirement_ids_for_comp,
            status_reason=_health_reason(health),
        )

    # -----------------------------------------------------------------
    # Step 3 — requirement nodes
    # -----------------------------------------------------------------
    for req_id, req in REQUIREMENT_REGISTRY.items():
        node_id = f"req:{req_id}"
        result = req_by_id.get(req_id)

        # A requirement's grouping parent is the component it belongs to
        # (if any); requirements also appear under subsystems that list
        # their ID in requirement_ids (already handled via the subsystem's
        # requirement_ids field — we don't duplicate into grouping_parents
        # to avoid UI-tree ambiguity).
        grouping: list[str] = []
        if req.component:
            grouping.append(f"component:{req.component}")

        nodes[node_id] = ControlNode(
            id=node_id,
            kind="requirement",
            label=req.description,
            description=req.fix_hint or req.description,
            grouping_parents=grouping,
            preference=None,
            effective_state="unknown",  # filled in step 5
            component_id=req.component,
            status_reason=(result.detail if result else "Check not yet run"),
        )

    # -----------------------------------------------------------------
    # Step 4 — capability nodes
    # -----------------------------------------------------------------
    # Capabilities exist as graph nodes (so the resolver can walk them
    # and the UI can list a component's `affects_capabilities`), but
    # they do NOT get a grouping_parent. A flat list of ~170 capabilities
    # is not useful in the user-facing domain tree — they surface via
    # the inverse edge on each component node instead.

    for name, entry in registry.items():
        node_id = f"cap:{name}"
        if isinstance(entry, Capability):
            description = entry.description
            requires = list(entry.requires)
        elif isinstance(entry, WorkflowDefinition):
            description = entry.description
            requires = list(entry.requires)
        else:  # pragma: no cover — defensive
            continue

        dep_edges = [
            Edge(target_id=f"component:{t_id}")
            for t_id in requires
            if f"component:{t_id}" in nodes or t_id in COMPONENT_CATALOG
        ]

        nodes[node_id] = ControlNode(
            id=node_id,
            kind="capability",
            label=name,
            description=description,
            grouping_parents=[],  # intentionally unparented — see note above
            dependencies=dep_edges,
            preference=None,
            effective_state="unknown",  # filled in step 5
        )

    # -----------------------------------------------------------------
    # Step 5 — derive effective_state (leaves first, then roll up)
    # -----------------------------------------------------------------
    # Order: components → requirements → capabilities → subsystems → domains
    # (so every parent sees resolved children).

    def _resolve(node_id: str) -> EffectiveState:
        node = nodes[node_id]
        state, reason, blockers = _derive_state(
            node=node,
            nodes=nodes,
            health_by_id=health_by_id,
            req_by_id=req_by_id,
        )
        node.effective_state = state
        if reason and not node.status_reason:
            node.status_reason = reason
        if blockers:
            node.blocking_issues = blockers
        return state

    # Components first (leaves in the runtime-dep graph)
    _resolve_in_dep_order(
        [nid for nid, n in nodes.items() if n.kind == "component"],
        nodes,
        _resolve,
    )
    # Requirements (depend only on their owning component's preference)
    for nid, n in nodes.items():
        if n.kind == "requirement":
            _resolve(nid)
    # Capabilities
    for nid, n in nodes.items():
        if n.kind == "capability":
            _resolve(nid)
    # Subsystems
    for nid, n in nodes.items():
        if n.kind == "subsystem":
            _resolve(nid)
    # Domains
    for nid, n in nodes.items():
        if n.kind == "domain":
            _resolve(nid)

    # -----------------------------------------------------------------
    # Step 6 — populate affects_capabilities on component nodes
    # -----------------------------------------------------------------
    # Only run if registry loaded successfully (otherwise empty).
    if registry:
        for comp_id in COMPONENT_CATALOG:
            affected: list[str] = []
            for cap_name, entry in registry.items():
                cap_node = nodes.get(f"cap:{cap_name}")
                if not cap_node:
                    continue
                # One-hop check against the capability's direct requires.
                # Full transitive closure via resolve_dependencies is
                # O(V*E) per component; deferred to Phase B.
                requires = (
                    list(entry.requires)
                    if isinstance(entry, (Capability, WorkflowDefinition))
                    else []
                )
                if comp_id in requires:
                    affected.append(cap_name)
            if affected:
                nodes[f"component:{comp_id}"].affects_capabilities = sorted(affected)

    return nodes


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------

def _derive_state(
    node: ControlNode,
    nodes: dict[str, ControlNode],
    health_by_id: dict[str, dict[str, Any]],
    req_by_id: dict[str, Any],
) -> tuple[EffectiveState, str, list[str]]:
    """Return ``(effective_state, status_reason, blocking_issues)`` for a node.

    Order of checks — EARLY RETURNS matter:

        1. preference='unwanted' → disabled (the cascade rule)
        2. any hard dependency disabled → disabled  (cascade propagates)
        3. any hard dependency not-ok → blocked
        4. kind-specific: component (from health), requirement (from result),
           capability (derived from deps alone), subsystem/domain (roll up)
    """
    # Rule 1: preference cascade. Components carry preference directly;
    # other kinds inherit from their component_id if present.
    effective_pref: Preference | None = node.preference
    if effective_pref is None and node.component_id:
        comp_node = nodes.get(f"component:{node.component_id}")
        if comp_node is not None:
            effective_pref = comp_node.preference

    if effective_pref == "unwanted":
        return ("disabled", "Opted out via preferences", [])

    # Dependency cascade — hard deps can block, soft deps can only
    # degrade. Both kinds respect the "disabled children mean I'm fine"
    # rule for soft edges (soft-dep disabled = we just don't use that
    # optional feature) but not for hard edges (hard-dep disabled =
    # we're genuinely blocked — the thing we need doesn't exist).
    hard_edges = [
        e for e in node.dependencies
        if e.target_id in nodes and e.mode == "all" and e.hardness == "hard"
    ]
    soft_edges = [
        e for e in node.dependencies
        if e.target_id in nodes and e.mode == "all" and e.hardness == "soft"
    ]

    # ---- Hard-dep cascade ----
    if hard_edges:
        hard_states = [nodes[e.target_id].effective_state for e in hard_edges]
        # All hard deps disabled → we're disabled too (nothing upstream
        # to use, so we're effectively off)
        if hard_states and all(s == "disabled" for s in hard_states):
            return ("disabled", "All hard dependencies are disabled", [])
        # Some (but not all) hard deps disabled → still blocked
        disabled_hard = [
            e.target_id for e in hard_edges
            if nodes[e.target_id].effective_state == "disabled"
        ]
        if disabled_hard:
            return (
                "blocked",
                f"Blocked: hard dependency disabled ({', '.join(disabled_hard)})",
                disabled_hard,
            )
        # Any hard dep not-ok → blocked
        hard_blockers = [
            e.target_id for e in hard_edges
            if nodes[e.target_id].effective_state
            in ("blocked", "unconfigured", "degraded", "unknown")
        ]
        if hard_blockers:
            return (
                "blocked",
                f"Blocked: {', '.join(hard_blockers)}",
                hard_blockers,
            )

    # ---- Soft-dep cascade ----
    # Soft deps being `disabled` is a non-event (we just don't use that
    # optional path). But soft deps in other unhealthy states make this
    # node at most `degraded`.
    soft_degraders = [
        e.target_id for e in soft_edges
        if nodes[e.target_id].effective_state
        in ("blocked", "unconfigured", "degraded", "unknown")
    ]
    if soft_degraders:
        # Stash for later — we still need to finish the kind-specific
        # derivation to decide if the node is already in worse shape
        # than degraded for some other reason.
        _soft_degradation_reason = (
            f"Operating without: {', '.join(soft_degraders)}"
        )
        _soft_degradation_list = soft_degraders
    else:
        _soft_degradation_reason = ""
        _soft_degradation_list = []

    # Rule 4: kind-specific derivation. If soft deps are unhealthy, we
    # "soften" the kind-specific result: an otherwise-ok node becomes
    # degraded; an already-worse node keeps its worse state.
    def _soften(state: EffectiveState, reason: str, blockers: list[str]) -> tuple[EffectiveState, str, list[str]]:
        if not _soft_degradation_list:
            return (state, reason, blockers)
        rank = {
            "blocked": 0, "unconfigured": 1, "degraded": 2,
            "unknown": 3, "ok": 4, "disabled": 5,
        }
        # If the kind-specific state is already worse than or equal to
        # degraded, keep it — don't paper over a real problem with a
        # soft-dep degradation message.
        if rank.get(state, 99) <= rank["degraded"]:
            return (state, reason, blockers)
        # Otherwise (ok/unknown/disabled with live soft-dep issue)
        merged_reason = _soft_degradation_reason
        if reason and reason != merged_reason:
            merged_reason = f"{reason}; also {_soft_degradation_reason}"
        return ("degraded", merged_reason, _soft_degradation_list)

    if node.kind == "component":
        return _soften(*_derive_component_state(node, health_by_id))

    if node.kind == "requirement":
        return _derive_requirement_state(node, req_by_id)

    if node.kind == "capability":
        # Deps-ok and preference-ok by this point → capability is ok
        return _soften("ok", "", [])

    if node.kind in ("subsystem", "domain"):
        return _soften(*_rollup_grouping(node, nodes))

    return ("unknown", "", [])


def _derive_component_state(
    node: ControlNode,
    health_by_id: dict[str, dict[str, Any]],
) -> tuple[EffectiveState, str, list[str]]:
    """Map HealthEngine status → EffectiveState for a component node."""
    assert node.component_id is not None
    h = health_by_id.get(node.component_id, {})
    status = h.get("status", "unknown")
    reason = _health_reason(h)

    mapping: dict[str, EffectiveState] = {
        "healthy": "ok",
        "degraded": "degraded",
        "unavailable": "degraded",
        "crashed": "degraded",
        "unhealthy": "degraded",
        "disabled": "disabled",
        "blocked": "blocked",
        "unknown": "unknown",
    }
    return (mapping.get(status, "unknown"), reason, [])


def _derive_requirement_state(
    node: ControlNode,
    req_by_id: dict[str, Any],
) -> tuple[EffectiveState, str, list[str]]:
    """Map RequirementResult → EffectiveState for a requirement node."""
    # The node.id has form "req:<requirement_id>" — strip the prefix.
    raw_id = node.id.removeprefix("req:")
    result = req_by_id.get(raw_id)
    if result is None:
        return ("unknown", "Check not yet run", [])
    if result.ok:
        return ("ok", result.detail or "", [])
    severity = result.severity
    if severity == "required":
        return ("unconfigured", result.detail or "Required check failed", [])
    return ("degraded", result.detail or "Recommended check failed", [])


def _rollup_grouping(
    node: ControlNode,
    nodes: dict[str, ControlNode],
) -> tuple[EffectiveState, str, list[str]]:
    """Roll up children's effective_state for a subsystem or domain node.

    Worst-child-wins ordering (rank low = worse):

        blocked > unconfigured > degraded > unknown > ok > disabled
    """
    # Find children: any node whose grouping_parents contains this node's id,
    # plus requirement_ids and dependency targets that sit under this node.
    children = [
        n for n in nodes.values() if node.id in n.grouping_parents
    ]
    # Include explicit requirement_ids (subsystems list them)
    children += [
        nodes[rid] for rid in node.requirement_ids if rid in nodes
    ]
    # Include dependency targets (so a subsystem's state reflects the
    # component it depends on, even if the component's grouping_parents
    # points elsewhere via multi-parenting).
    for e in node.dependencies:
        if e.target_id in nodes and nodes[e.target_id] not in children:
            children.append(nodes[e.target_id])

    if not children:
        return ("unknown", "No children", [])

    # Rank: lower = worse
    rank = {
        "blocked": 0,
        "unconfigured": 1,
        "degraded": 2,
        "unknown": 3,
        "ok": 4,
        "disabled": 5,
    }
    child_states = [c.effective_state for c in children]
    if all(s == "disabled" for s in child_states):
        return ("disabled", "All children disabled", [])
    # Exclude disabled children from the worst-wins comparison
    considered = [s for s in child_states if s != "disabled"]
    worst = min(considered, key=lambda s: rank.get(s, 99))
    return (worst, "", [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preference_from_obj(pref_obj: Any, is_core: bool = False) -> Preference | None:
    """Map a ``FeaturePreference`` dataclass to the preference enum.

    Core components always return ``"required"`` regardless of what the
    user wrote in config.local.yaml — ``is_core`` trumps any explicit
    opt-out, matching ``preferences.is_wanted()`` semantics.
    """
    if is_core:
        return "required"
    if pref_obj is None:
        return "undecided"
    wanted = getattr(pref_obj, "wanted", None)
    if wanted is True:
        return "wanted"
    if wanted is False:
        return "unwanted"
    return "undecided"


def _health_reason(health: dict[str, Any]) -> str:
    """Extract a one-line reason from a HealthEngine component entry."""
    if not health:
        return ""
    details = health.get("details") or {}
    reason = details.get("reason") or details.get("probe_reason") or ""
    status = health.get("status", "")
    if reason:
        return str(reason)
    if status and status != "healthy":
        return f"Status: {status}"
    return ""


def _resolve_in_dep_order(
    node_ids: list[str],
    nodes: dict[str, ControlNode],
    resolve_fn,
) -> None:
    """Resolve component nodes in dependency order (deps first).

    Uses a simple topological pass over ``dependencies`` edges; cycles
    are broken by falling back to iteration order (not expected in the
    current COMPONENT_CATALOG).
    """
    resolved: set[str] = set()
    pending = list(node_ids)

    # Up to N full passes — each pass resolves every node whose deps are
    # already resolved. Short-circuits cleanly on the small COMPONENT_CATALOG.
    for _ in range(len(pending) + 1):
        progress = False
        still_pending: list[str] = []
        for nid in pending:
            node = nodes[nid]
            dep_ids_in_same_set = [
                e.target_id
                for e in node.dependencies
                if e.target_id in node_ids
            ]
            if all(d in resolved for d in dep_ids_in_same_set):
                resolve_fn(nid)
                resolved.add(nid)
                progress = True
            else:
                still_pending.append(nid)
        pending = still_pending
        if not pending or not progress:
            break

    # Any leftover (cycle) — resolve in arbitrary order
    for nid in pending:
        resolve_fn(nid)
