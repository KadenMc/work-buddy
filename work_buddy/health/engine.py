"""HealthEngine — merges tool probes and sidecar state into unified health view.

Two-tier health merge:
    - tools.py (application-level): "can I talk to the API?" via tool_status.json
    - sidecar/state.py (process-level): "is the PID alive?" via sidecar_state.json

The engine reads cached files only — it never runs probes itself. This makes
it safe to call from any process (dashboard, MCP server, CLI).

Status matrix:
    tool_probe   | sidecar      | merged status
    -------------|--------------|---------------
    available    | healthy      | healthy
    available    | (no entry)   | healthy        (standalone tools)
    unavailable  | crashed      | crashed
    unavailable  | healthy      | degraded       (process alive, API down)
    unavailable  | (no entry)   | unavailable
    (disabled)   | any          | disabled
    (blocked)    | any          | blocked        (parent dependency unhealthy)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from work_buddy.paths import resolve

_TOOL_STATUS_FILE = resolve("runtime/tool-status")
_SIDECAR_STATE_FILE = resolve("runtime/sidecar-state")


@dataclass
class ComponentHealth:
    """Unified health status for a single component."""

    id: str
    display_name: str
    category: str
    status: str  # healthy, degraded, unavailable, crashed, disabled, blocked, unknown
    wanted: bool | None = None  # True/False/None from user preferences
    depends_on: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HealthEngine:
    """Reads cached health data and merges into a unified component view."""

    def __init__(self) -> None:
        self._tool_status: dict[str, dict[str, Any]] = {}
        self._sidecar_services: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        """Load cached data from disk. Idempotent within one call."""
        if self._loaded:
            return
        self._loaded = True

        # Tool probe results (written by tools.py:probe_all)
        if _TOOL_STATUS_FILE.exists():
            try:
                self._tool_status = json.loads(
                    _TOOL_STATUS_FILE.read_text(encoding="utf-8")
                )
            except Exception as exc:
                log.warning("Failed to read tool_status.json: %s", exc)

        # Sidecar state (written by sidecar daemon)
        if _SIDECAR_STATE_FILE.exists():
            try:
                data = json.loads(
                    _SIDECAR_STATE_FILE.read_text(encoding="utf-8")
                )
                self._sidecar_services = data.get("services", {})
            except Exception as exc:
                log.warning("Failed to read sidecar_state.json: %s", exc)

    def _merge_status(self, comp_id: str, health_source: str,
                      sidecar_service: str | None) -> tuple[str, dict[str, Any]]:
        """Determine merged status for a component.

        Returns (status_string, details_dict).
        """
        tool = self._tool_status.get(comp_id, {})
        svc = self._sidecar_services.get(sidecar_service or "", {}) if sidecar_service else {}

        details: dict[str, Any] = {}

        # Config disabled?
        if tool.get("config_enabled") is False:
            return "disabled", {"reason": tool.get("reason", "Disabled in config")}

        tool_available = tool.get("available", False)
        svc_status = svc.get("status", "") if svc else ""

        if tool:
            details["probe_ms"] = tool.get("probe_ms", 0)
            details["probe_reason"] = tool.get("reason", "")
        if svc:
            details["sidecar_status"] = svc_status
            details["sidecar_pid"] = svc.get("pid")
            details["crash_count"] = svc.get("crash_count", 0)

        if health_source == "tool_probe":
            if tool_available:
                return "healthy", details
            reason = tool.get("reason", "")
            if "Dependency unavailable" in reason:
                return "blocked", details
            return "unavailable", details

        if health_source == "sidecar":
            if svc_status == "healthy":
                return "healthy", details
            if svc_status == "crashed":
                return "crashed", details
            if svc_status in ("stopped", ""):
                return "unavailable", details
            return "unhealthy", details

        if health_source == "composite":
            if tool_available and svc_status in ("healthy", ""):
                return "healthy", details
            if not tool_available and svc_status == "crashed":
                return "crashed", details
            if not tool_available and svc_status == "healthy":
                # The Hindsight half-dead case: process alive but API down
                return "degraded", details
            if tool_available:
                return "healthy", details
            reason = tool.get("reason", "")
            if "Dependency unavailable" in reason:
                return "blocked", details
            return "unavailable", details

        if health_source == "custom":
            # Custom components don't have probes or sidecar entries —
            # status is always "unknown" from the engine's perspective.
            # DiagnosticRunner determines actual status via check_sequence.
            return "unknown", details

        return "unknown", details

    def get_all(self) -> dict[str, Any]:
        """Return unified health for all registered components.

        Returns::

            {
                "components": [ComponentHealth.to_dict(), ...],
                "summary": {"healthy": N, "unhealthy": N, "disabled": N, "opted_out": N, "total": N},
            }
        """
        from work_buddy.health.components import COMPONENT_CATALOG
        from work_buddy.health.preferences import load_preferences

        self._load()
        prefs = load_preferences()

        components: list[ComponentHealth] = []
        status_counts: dict[str, int] = {}

        # Build child map for parent→children linking
        child_map: dict[str, list[str]] = {}
        for comp in COMPONENT_CATALOG.values():
            for dep in comp.depends_on:
                child_map.setdefault(dep, []).append(comp.id)

        # Resolve statuses
        resolved: dict[str, str] = {}
        for comp in COMPONENT_CATALOG.values():
            pref = prefs.get(comp.id)
            wanted = pref.wanted if pref else None

            status, details = self._merge_status(
                comp.id, comp.health_source, comp.sidecar_service
            )

            # If user opted out, mark as disabled regardless of probe state
            if wanted is False and status != "disabled":
                status = "disabled"
                details["reason"] = details.get("reason", "User opted out")
                if "user_opted_out" not in details:
                    details["user_opted_out"] = True

            # If any parent is unhealthy, mark as blocked
            if status not in ("disabled", "blocked"):
                for dep_id in comp.depends_on:
                    dep_status = resolved.get(dep_id, "unknown")
                    if dep_status not in ("healthy", "disabled", "unknown"):
                        status = "blocked"
                        details["blocked_by"] = dep_id
                        break

            resolved[comp.id] = status

            ch = ComponentHealth(
                id=comp.id,
                display_name=comp.display_name,
                category=comp.category,
                status=status,
                wanted=wanted,
                depends_on=comp.depends_on,
                details=details,
                children=child_map.get(comp.id, []),
            )
            components.append(ch)
            status_counts[status] = status_counts.get(status, 0) + 1

        healthy = status_counts.get("healthy", 0)
        disabled = status_counts.get("disabled", 0)
        opted_out = sum(1 for c in components if c.wanted is False)
        total = len(components)
        unhealthy = total - healthy - disabled

        return {
            "components": [c.to_dict() for c in components],
            "summary": {
                "healthy": healthy,
                "unhealthy": unhealthy,
                "disabled": disabled,
                "opted_out": opted_out,
                "total": total,
            },
        }

    def get_component(self, component_id: str) -> ComponentHealth | None:
        """Get health for a single component."""
        from work_buddy.health.components import COMPONENT_CATALOG
        from work_buddy.health.preferences import is_wanted

        self._load()
        comp = COMPONENT_CATALOG.get(component_id)
        if comp is None:
            return None

        wanted = is_wanted(comp.id)
        status, details = self._merge_status(
            comp.id, comp.health_source, comp.sidecar_service
        )

        if wanted is False and status != "disabled":
            status = "disabled"
            details["user_opted_out"] = True

        child_map: dict[str, list[str]] = {}
        for c in COMPONENT_CATALOG.values():
            for dep in c.depends_on:
                child_map.setdefault(dep, []).append(c.id)

        return ComponentHealth(
            id=comp.id,
            display_name=comp.display_name,
            category=comp.category,
            status=status,
            wanted=wanted,
            depends_on=comp.depends_on,
            details=details,
            children=child_map.get(comp.id, []),
        )
