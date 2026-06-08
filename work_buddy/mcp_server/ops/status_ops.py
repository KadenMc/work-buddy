"""Status-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op



def _register() -> None:
    from work_buddy.mcp_server.registry import reload_capability_data

    from work_buddy.messaging import client
    from work_buddy import agent_session
    from work_buddy.mcp_server.tools.gateway import retry_operation as _retry_operation

    def _tailscale_status() -> dict:
        """Check Tailscale daemon status and Serve configuration.

        Thin wrapper over the shared helper in ``work_buddy.health.checks``;
        the helper is also used by the requirement and component health
        checks so a single ``setup_help`` invocation only shells out once.
        """
        from work_buddy.health.checks import get_tailscale_status
        return get_tailscale_status()

    def _feature_status(verbose: bool = False, force: bool = False) -> dict:
        """Show which tools, features, and capabilities are available or disabled.

        When ``force=True``, re-runs every tool probe fresh rather than
        reading the cached result from the last probe sweep. Use this
        when you suspect a cached "unavailable" is stale — e.g., the
        user just started Obsidian and wants to confirm the bridge is
        now up.
        """
        from work_buddy.tools import get_tool_status, probe_all
        from work_buddy.health.preferences import load_preferences
        from work_buddy.health.requirements import RequirementChecker

        if force:
            probe_all(force=True)

        result = get_tool_status()
        if not verbose:
            # Compact: just tool names and disabled capability names
            result["tools"] = {
                tid: {"available": s["available"], "reason": s.get("reason", "")}
                for tid, s in result.get("tools", {}).items()
            }

        # Include user preferences
        prefs = load_preferences()
        result["preferences"] = {
            comp_id: pref.to_dict()
            for comp_id, pref in prefs.items()
        }

        # Include requirement summary (lightweight)
        try:
            checker = RequirementChecker()
            req_results = checker.check_bootstrap()
            result["bootstrap_requirements"] = checker.summarize(req_results)
        except Exception:
            result["bootstrap_requirements"] = {"error": "Could not check requirements"}

        return result

    def _setup_wizard(
        mode: str = "status",
        component: str = "",
        updates: dict | None = None,
    ) -> dict:
        """Setup wizard — comprehensive setup, diagnostics, and preferences.

        Modes:
            status      — Quick health + requirements overview (default).
            guided      — Interactive first-time setup with structured steps.
            diagnose    — Deep diagnostic for a specific component.
            preferences — View/edit feature preferences.
        """
        from work_buddy.health.wizard import SetupWizard
        wizard = SetupWizard()

        if mode == "guided":
            return wizard.guided()
        elif mode == "diagnose":
            if not component:
                return {"error": "diagnose mode requires a component parameter"}
            return wizard.diagnose(component)
        elif mode == "preferences":
            return wizard.preferences(updates=updates)
        else:
            return wizard.status()

    def _setup_help(component: str = "all") -> dict:
        """Diagnose component health. Runs automated check sequences.

        If a specific component is given, walks its dependency chain and
        runs diagnostic checks — stopping at the first failure with a
        root cause and fix suggestion.

        If "all" (default), returns a health overview of all components
        with any unhealthy ones highlighted.
        """
        from work_buddy.health import HealthEngine, DiagnosticRunner
        from work_buddy.health.components import COMPONENT_CATALOG

        if component != "all" and component in COMPONENT_CATALOG:
            runner = DiagnosticRunner()
            result = runner.diagnose(component)
            # Also include the engine's merged status for context
            engine = HealthEngine()
            health = engine.get_component(component)
            return {
                "mode": "diagnose",
                "component": component,
                "engine_status": health.to_dict() if health else None,
                "diagnostic": result.to_dict(),
            }

        # Overview mode
        engine = HealthEngine()
        overview = engine.get_all()

        # Run diagnostics only on unhealthy components
        unhealthy_ids = [
            c["id"] for c in overview["components"]
            if c["status"] not in ("healthy", "disabled")
        ]
        diagnostics = {}
        if unhealthy_ids:
            runner = DiagnosticRunner()
            for cid in unhealthy_ids:
                diagnostics[cid] = runner.diagnose(cid).to_dict()

        return {
            "mode": "overview",
            "summary": overview["summary"],
            "components": overview["components"],
            "diagnostics": diagnostics,
            "available_components": sorted(COMPONENT_CATALOG.keys()),
        }

    register_op("op.wb.feature_status", _feature_status)
    register_op("op.wb.setup_help", _setup_help)
    register_op("op.wb.setup_wizard", _setup_wizard)
    register_op("op.wb.service_health", client.is_service_running)
    register_op("op.wb.list_sessions", agent_session.list_sessions)
    register_op("op.wb.reload_capability_data", reload_capability_data)
    register_op("op.wb.retry", _retry_operation)
    register_op("op.wb.tailscale_status", _tailscale_status)


_register()
