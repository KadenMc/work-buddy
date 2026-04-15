"""Setup Wizard — orchestrates preferences, requirements, health, and diagnostics.

The wizard capability supports four modes:

    status    — Quick health + requirements summary for wanted components.
    guided    — Interactive first-time setup (returns structured steps).
    diagnose  — Deep diagnostic for a specific component.
    preferences — View/edit feature preferences.

The wizard is an MCP capability, not a workflow.  It returns structured data
for the agent to present interactively — the agent drives the conversation.
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
        """
        from work_buddy.health.components import COMPONENT_CATALOG
        from work_buddy.health.preferences import load_preferences

        # Step 1: Bootstrap checks
        bootstrap = self.requirements.check_bootstrap()
        bootstrap_summary = self.requirements.summarize(bootstrap)

        # Step 2: Feature inventory — all components grouped by category
        prefs = load_preferences()
        components_by_category: dict[str, list[dict[str, Any]]] = {}
        for comp in COMPONENT_CATALOG.values():
            pref = prefs.get(comp.id)
            entry = {
                "id": comp.id,
                "display_name": comp.display_name,
                "category": comp.category,
                "wanted": pref.wanted if pref else None,
                "reason": pref.reason if pref else None,
                "depends_on": comp.depends_on,
            }
            components_by_category.setdefault(comp.category, []).append(entry)

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
                    "description": "Choose which components you want to use.",
                    "components": components_by_category,
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
                "2. Ask which features they want — save preferences with set_preference().\n"
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
        """
        from work_buddy.health.preferences import (
            load_preferences,
            set_preference,
        )
        from work_buddy.health.components import COMPONENT_CATALOG

        # Apply updates if provided
        if updates:
            for comp_id, data in updates.items():
                if comp_id not in COMPONENT_CATALOG:
                    continue
                if isinstance(data, dict):
                    set_preference(
                        comp_id,
                        wanted=data.get("wanted"),
                        reason=data.get("reason"),
                    )
                elif isinstance(data, bool) or data is None:
                    set_preference(comp_id, wanted=data)

        # Return current state
        prefs = load_preferences()
        components = []
        for comp in COMPONENT_CATALOG.values():
            pref = prefs.get(comp.id)
            components.append({
                "id": comp.id,
                "display_name": comp.display_name,
                "category": comp.category,
                "wanted": pref.wanted if pref else None,
                "reason": pref.reason if pref else None,
            })

        return {
            "mode": "preferences",
            "components": components,
            "updated": bool(updates),
        }
