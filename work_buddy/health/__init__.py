"""Unified health/diagnostics subsystem for work-buddy.

Four layers:
    1. ComponentRegistry (components.py) — catalog of all monitored components
    2. HealthEngine (engine.py) — merges tool probes + sidecar state
    3. DiagnosticRunner (diagnostics.py) — troubleshooting check sequences
    4. Surfaces — MCP capability (setup_help), slash command, dashboard API

Usage::

    from work_buddy.health import HealthEngine, DiagnosticRunner

    # Quick overview
    engine = HealthEngine()
    health = engine.get_all()

    # Diagnose a specific component
    runner = DiagnosticRunner()
    result = runner.diagnose("hindsight")
"""

from work_buddy.health.components import COMPONENT_CATALOG, CheckStep, ComponentDef
from work_buddy.health.diagnostics import DiagnosticResult, DiagnosticRunner, StepResult
from work_buddy.health.engine import ComponentHealth, HealthEngine
from work_buddy.health.preferences import FeaturePreference
from work_buddy.health.requirements import (
    REQUIREMENT_REGISTRY,
    RequirementChecker,
    RequirementDef,
    RequirementResult,
)

__all__ = [
    "COMPONENT_CATALOG",
    "CheckStep",
    "ComponentDef",
    "ComponentHealth",
    "DiagnosticResult",
    "DiagnosticRunner",
    "FeaturePreference",
    "HealthEngine",
    "REQUIREMENT_REGISTRY",
    "RequirementChecker",
    "RequirementDef",
    "RequirementResult",
    "StepResult",
]
