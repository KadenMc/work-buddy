"""Setup Wizard — orchestrates preferences, requirements, health, and diagnostics.

The wizard capability supports four modes:

    status    — Quick health + requirements summary for wanted components.
    guided    — Interactive first-time setup (returns structured steps).
    diagnose  — Deep diagnostic for a specific component.
    preferences — View/edit feature preferences.

The wizard is an MCP capability, not a workflow.  It returns structured data
for the agent to present interactively — the agent drives the conversation.

Phase G (2026-04): ``guided()`` and ``preferences()`` now consume the
control graph (``work_buddy.control.graph``) for their user-facing
grouping, rather than the legacy ``ComponentDef.category`` flat buckets.
The category field is still surfaced as a hint for backward compat.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class SetupWizard:
    """Orchestrates the full setup wizard flow."""

    def __init__(self) -> None:
        from work_buddy.health.engine import HealthEngine
        from work_buddy.health.diagnostics import DiagnosticRunner
        from work_buddy.health.requirements import RequirementChecker

        self.engine = HealthEngine()
        self.diagnostics = DiagnosticRunner()
        self.requirements = RequirementChecker()

    def status(self) -> dict[str, Any]:
        """Quick health + requirements overview for all wanted components.

        Returns bootstrap check results, component health, and requirement
        validation — everything an agent needs to understand the current state.
        """
        # 1. Bootstrap checks (always run)
        bootstrap = self.requirements.check_bootstrap()
        bootstrap_summary = self.requirements.summarize(bootstrap)

        # 2. Component health (respects preferences)
        health = self.engine.get_all()

        # 3. Requirement checks for wanted components
        req_results = self.requirements.check_all(include_unwanted=False)
        req_summary = self.requirements.summarize(req_results)

        return {
            "mode": "status",
            "bootstrap": {
                "summary": bootstrap_summary,
                "results": [r.to_dict() for r in bootstrap],
            },
            "health": health,
            "requirements": {
                "summary": req_summary,
                "results": [r.to_dict() for r in req_results],
            },
        }

    def guided(self) -> dict[str, Any]:
        """Interactive guided setup — returns structured steps for the agent.

        The agent walks the user through each step, collecting choices and
        applying fixes.  Each step includes all the data needed to present
        it to the user.

        Step 2 ("features") is organized by user-facing **domains** derived
        from the control graph (Journal, Notifications, Knowledge & Retrieval,
        etc.), not the legacy implementation-centric category buckets
        (external/integration/service/plugin). This matches what the
        Settings tab shows and gives the agent a natural conversation path.
        """
        from work_buddy.health.preferences import load_preferences

        # Step 1: Bootstrap checks
        bootstrap = self.requirements.check_bootstrap()
        bootstrap_summary = self.requirements.summarize(bootstrap)

        # Step 2: Feature inventory — grouped by control-graph domain
        prefs = load_preferences()
        domains_view, category_view = _build_feature_inventory(prefs)

        # Step 3: Requirement checks for wanted components
        req_results = self.requirements.check_all(include_unwanted=False)
        req_summary = self.requirements.summarize(req_results)

        # Step 4: Health check for wanted components
        health = self.engine.get_all()
        wanted_health = [
            c for c in health["components"]
            if c.get("wanted") is not False
        ]

        return {
            "mode": "guided",
            "steps": [
                {
                    "step": 1,
                    "name": "bootstrap",
                    "title": "Core Requirements",
                    "description": "Validate fundamental work-buddy configuration.",
                    "summary": bootstrap_summary,
                    "results": [r.to_dict() for r in bootstrap],
                },
                {
                    "step": 2,
                    "name": "features",
                    "title": "Feature Selection",
                    "description": (
                        "Choose which components you want to use, grouped "
                        "into user-facing domains (Journal, Notifications, "
                        "Knowledge, etc.)."
                    ),
                    # Primary: domain-grouped view (post-Phase-G).
                    "domains": domains_view,
                    # Backward-compat: legacy category buckets, kept so older
                    # consumers of the guided output keep working. Delete
                    # once nothing reads it.
                    "components": category_view,
                },
                {
                    "step": 3,
                    "name": "requirements",
                    "title": "Configuration Validation",
                    "description": "Check that wanted features are configured correctly.",
                    "summary": req_summary,
                    "results": [r.to_dict() for r in req_results],
                },
                {
                    "step": 4,
                    "name": "health",
                    "title": "Service Health",
                    "description": "Verify running services for wanted features.",
                    "summary": health["summary"],
                    "components": wanted_health,
                },
            ],
            "instructions": (
                "Walk the user through each step:\n"
                "1. Fix any bootstrap failures first (these block everything).\n"
                "2. Walk the user through each DOMAIN in the features step "
                "— for each, confirm which components they want. Save with "
                "set_preference() or apply_preference_updates().\n"
                "3. Show requirement failures with fix hints — help them resolve.\n"
                "4. Check service health — diagnose any unhealthy wanted components.\n"
                "After all steps, save preferences to config.local.yaml."
            ),
        }

    def diagnose(self, component: str) -> dict[str, Any]:
        """Deep diagnostic for a specific component.

        Combines requirements validation, health status, and diagnostic
        check sequences.  Equivalent to the existing ``setup_help`` behavior
        plus requirements checking.
        """
        from work_buddy.health.components import COMPONENT_CATALOG
        from work_buddy.health.preferences import get_preference

        comp = COMPONENT_CATALOG.get(component)
        if comp is None:
            available = sorted(COMPONENT_CATALOG.keys())
            return {
                "mode": "diagnose",
                "error": f"Unknown component: '{component}'",
                "available_components": available,
            }

        pref = get_preference(component)

        # Requirements for this component
        req_results = self.requirements.check_component(component)
        req_summary = self.requirements.summarize(req_results)

        # Health status
        health = self.engine.get_component(component)

        # Diagnostic check sequence
        diag = self.diagnostics.diagnose(component)

        return {
            "mode": "diagnose",
            "component": component,
            "display_name": comp.display_name,
            "preference": {
                "wanted": pref.wanted,
                "reason": pref.reason,
            },
            "requirements": {
                "summary": req_summary,
                "results": [r.to_dict() for r in req_results],
            },
            "health": health.to_dict() if health else None,
            "diagnostics": diag.to_dict() if hasattr(diag, "to_dict") else {
                "status": diag.status,
                "steps_run": [s.__dict__ for s in diag.steps_run],
                "root_cause": diag.root_cause,
                "fix_suggestion": diag.fix_suggestion,
            },
        }

    def preferences(
        self,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """View or edit feature preferences.

        If ``updates`` is provided, apply them first:
        ``{"hindsight": {"wanted": false, "reason": "..."}, ...}``

        Each component entry now carries a ``domains`` list (from the
        control graph) in addition to the legacy ``category`` field so
        callers can render user-facing groupings.
        """
        from work_buddy.health.preferences import (
            apply_preference_updates,
            load_preferences,
        )
        from work_buddy.health.components import COMPONENT_CATALOG

        # Apply updates if provided (consent-gated)
        if updates:
            apply_preference_updates(updates)

        # Domain lookup per component_id, from the control graph.
        domains_by_component = _component_domain_index()

        # Return current state
        prefs = load_preferences()
        components = []
        for comp in COMPONENT_CATALOG.values():
            pref = prefs.get(comp.id)
            components.append({
                "id": comp.id,
                "display_name": comp.display_name,
                "category": comp.category,  # legacy; kept for back-compat
                "domains": domains_by_component.get(comp.id, []),
                "wanted": pref.wanted if pref else None,
                "reason": pref.reason if pref else None,
            })

        return {
            "mode": "preferences",
            "components": components,
            "updated": bool(updates),
        }


# ---------------------------------------------------------------------------
# Module-level helpers (control-graph-driven feature inventory)
# ---------------------------------------------------------------------------

def _build_feature_inventory(
    prefs: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Return (domain-grouped view, legacy category view) of components.

    The domain view walks the control graph's domain → subsystem →
    component hierarchy. The legacy view is the old
    ``components_by_category`` dict kept for any caller that still reads
    it. A component that legitimately belongs to multiple domains
    (e.g. Obsidian under both Journal and Notifications) appears under
    each; the underlying preference is shared.
    """
    from work_buddy.health.components import COMPONENT_CATALOG

    # Try the control graph first; fall back to category-only view if
    # the graph can't be built for any reason (e.g. registry not
    # initialized in a narrow test context).
    try:
        from work_buddy.control.graph import build_graph
        graph = build_graph()
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("control graph unavailable, falling back: %s", exc)
        graph = {}

    # Legacy: flat by category. Includes every component unconditionally.
    category_view: dict[str, list[dict[str, Any]]] = {}
    for comp in COMPONENT_CATALOG.values():
        entry = _component_entry(comp, prefs)
        category_view.setdefault(comp.category, []).append(entry)

    if not graph:
        return [], category_view

    # Domain view: ordered, user-facing hierarchy.
    domain_order = [
        "domain:journal",
        "domain:notifications",
        "domain:knowledge",
        "domain:browser",
        "domain:calendar",
        "domain:runtime",
        "domain:system",
    ]
    all_domain_ids = [
        nid for nid, n in graph.items() if n.kind == "domain"
    ]
    ordered = [d for d in domain_order if d in graph] + [
        d for d in all_domain_ids if d not in domain_order
    ]

    domains_view: list[dict[str, Any]] = []
    for did in ordered:
        dnode = graph[did]
        subsystems = []
        direct_comp_ids: list[str] = []

        # Subsystems that live under this domain
        for nid, node in graph.items():
            if node.kind == "subsystem" and did in node.grouping_parents:
                sub_comp_entries = []
                for edge in node.dependencies:
                    target = graph.get(edge.target_id)
                    if target and target.kind == "component":
                        comp = COMPONENT_CATALOG.get(target.component_id or "")
                        if comp is not None:
                            sub_comp_entries.append(
                                _component_entry(comp, prefs)
                            )
                subsystems.append({
                    "id": node.id,
                    "label": node.label,
                    "description": node.description,
                    "effective_state": node.effective_state,
                    "components": sub_comp_entries,
                })

        # Components directly under the domain (not via subsystem)
        for nid, node in graph.items():
            if node.kind == "component" and did in node.grouping_parents:
                if node.component_id:
                    direct_comp_ids.append(node.component_id)
        direct_components = []
        for cid in dict.fromkeys(direct_comp_ids):  # dedupe, preserve order
            comp = COMPONENT_CATALOG.get(cid)
            if comp is not None:
                direct_components.append(_component_entry(comp, prefs))

        domains_view.append({
            "id": did,
            "label": dnode.label,
            "description": dnode.description,
            "effective_state": dnode.effective_state,
            "subsystems": subsystems,
            "direct_components": direct_components,
        })

    return domains_view, category_view


def _component_entry(comp, prefs: dict[str, Any]) -> dict[str, Any]:
    """Render a COMPONENT_CATALOG entry as a wizard-friendly dict."""
    pref = prefs.get(comp.id)
    return {
        "id": comp.id,
        "display_name": comp.display_name,
        "category": comp.category,  # legacy; still useful as a hint
        "wanted": pref.wanted if pref else None,
        "reason": pref.reason if pref else None,
        "depends_on": list(comp.depends_on),
    }


def _component_domain_index() -> dict[str, list[str]]:
    """Return ``{component_id: [domain_id, ...]}`` from the control graph.

    Used by ``preferences()`` to tag each component with the user-facing
    domains it belongs to. Returns an empty dict if the graph can't be
    built (wizard still works, just loses the hint).
    """
    try:
        from work_buddy.control.graph import build_graph
        graph = build_graph()
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("control graph unavailable for domain index: %s", exc)
        return {}

    out: dict[str, list[str]] = {}
    for node in graph.values():
        if node.kind != "component" or not node.component_id:
            continue
        domain_parents = [
            p for p in node.grouping_parents if p.startswith("domain:")
        ]
        if domain_parents:
            out[node.component_id] = domain_parents
    return out
