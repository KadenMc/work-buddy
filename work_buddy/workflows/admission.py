"""Composable Workflow admission rules shared by every driving adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from work_buddy.workflows.context import (
    ExecutorFacility,
    FeaturePreferenceContext,
    WorkflowEntrySurface,
    WorkflowInvocationContext,
)


@dataclass(frozen=True, slots=True)
class WorkflowAdmissionReason:
    """One structured reason a Workflow invocation was refused."""

    code: str
    message: str
    denied_by: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "denied_by": self.denied_by,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class WorkflowAdmissionDecision:
    """Aggregate result of evaluating all Workflow admission rules."""

    reasons: tuple[WorkflowAdmissionReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }

    def denial_payload(self, workflow: Any) -> dict[str, Any]:
        """Return a stable public payload while retaining every reason."""

        if self.allowed:
            return {"allowed": True, "admission": self.to_dict()}
        primary = self.reasons[0]
        payload: dict[str, Any] = {
            "error": primary.message,
            "error_code": primary.code,
            "denied_by": primary.denied_by,
            "workflow_id": getattr(workflow, "workflow_id", None),
            "workflow_name": getattr(workflow, "name", None),
            "admission": self.to_dict(),
        }
        payload.update(primary.details)
        return payload


PreferenceProvider = Callable[[Any], FeaturePreferenceContext]
ComponentProvider = Callable[[str], bool]


def _required_components(workflow: Any) -> tuple[str, ...]:
    compiled = getattr(workflow, "required_components", None)
    raw = compiled if compiled is not None else getattr(workflow, "requires", ())
    return tuple(sorted(set(raw or ())))


def _default_preferences(workflow: Any) -> FeaturePreferenceContext:
    from work_buddy.mcp_server.runtime_admission import evaluate_runtime_admission

    # Optional-only dependencies must not turn a skippable Step into a
    # whole-Workflow privacy/preference denial.
    result = evaluate_runtime_admission(
        SimpleNamespace(requires=_required_components(workflow))
    )
    return FeaturePreferenceContext(
        available=result.preference_available,
        opted_out=tuple(result.opted_out),
    )


def _default_component_available(component_id: str) -> bool:
    from work_buddy.tools import is_tool_available

    if is_tool_available(component_id):
        return True
    # Match the conductor's last-mile recovery behavior: a stale bootstrap
    # probe gets one cooldown-protected recheck before admission denies.
    from work_buddy.recovery import recheck_tool

    return bool(recheck_tool(component_id))


def _executor_requirement(step: Any) -> tuple[ExecutorFacility, ...]:
    """Return the current-schema facility alternatives for one Step."""

    # Only an explicit ``auto_run`` is executed by the conductor itself.
    # ``step_type=code`` without one is still handed back to the driving
    # executor, so its declared ``execution`` policy remains authoritative.
    if getattr(step, "auto_run", None) is not None:
        return (ExecutorFacility.PROGRAM,)
    execution = getattr(step, "execution", "main")
    if execution == "subagent":
        return (ExecutorFacility.SUBAGENT,)
    if execution == "main":
        return (ExecutorFacility.CALLING_AGENT,)
    return ()


class WorkflowAdmission:
    """Evaluate independent availability and authorization rules."""

    def __init__(
        self,
        *,
        preference_provider: PreferenceProvider = _default_preferences,
        component_provider: ComponentProvider = _default_component_available,
    ) -> None:
        self._preference_provider = preference_provider
        self._component_provider = component_provider

    def evaluate(
        self,
        workflow: Any,
        invocation_context: WorkflowInvocationContext,
    ) -> WorkflowAdmissionDecision:
        reasons: list[WorkflowAdmissionReason] = []

        authorization_reason = self._authorization_reason(
            getattr(workflow, "name", ""), invocation_context
        )
        if authorization_reason is not None:
            reasons.append(authorization_reason)

        if (
            invocation_context.entry_surface == WorkflowEntrySurface.SKILL_GATEWAY
            and invocation_context.skill_surface_eligible is False
        ):
            reasons.append(WorkflowAdmissionReason(
                code="skill_surface_ineligible",
                message="This Workflow is not published on the Skill gateway.",
                denied_by="skill_surface",
            ))

        gate_text = getattr(workflow, "available_when", None)
        if gate_text:
            from work_buddy.control import gates

            try:
                gate = gates.parse_gate(gate_text)
            except ValueError:
                # Authored gates are validated during registry compilation.
                # Preserve the historical fail-open behavior for a stale or
                # pre-validation cached object rather than manufacturing a
                # policy interpretation here.
                gate = None
            if gate is not None and not gates.evaluate(gate, set(invocation_context.active_modes)):
                required = sorted(gates.referenced_components(gate))
                reasons.append(WorkflowAdmissionReason(
                    code="mode_not_active",
                    message=(
                        f"Workflow {getattr(workflow, 'name', '')!r} requires "
                        f"mode(s) {required} that are not active. Enable with mode_toggle."
                    ),
                    denied_by="mode_gate",
                    details={
                        "disabled": True,
                        "required_modes": required,
                        "active_modes": sorted(invocation_context.active_modes),
                    },
                ))

        available_in = set(getattr(workflow, "available_in", ()) or ())
        active_invocation_value = getattr(
            invocation_context.invocation_context,
            "value",
            str(invocation_context.invocation_context),
        )
        available_values = {
            getattr(item, "value", str(item))
            for item in available_in
        }
        if available_values and active_invocation_value not in available_values:
            reasons.append(WorkflowAdmissionReason(
                code="invocation_context_denied",
                message=(
                    f"Workflow {getattr(workflow, 'name', '')!r} is unavailable "
                    f"in invocation context {active_invocation_value!r}."
                ),
                denied_by="invocation_context",
                details={
                    "invocation_context": active_invocation_value,
                    "available_in": sorted(available_values),
                },
            ))

        required_components = _required_components(workflow)
        try:
            preferences = (
                invocation_context.feature_preferences
                if invocation_context.feature_preferences is not None
                else self._preference_provider(workflow)
            )
        except Exception:
            preferences = FeaturePreferenceContext(available=False)
        if not preferences.available:
            reasons.append(WorkflowAdmissionReason(
                code="feature_preference_unavailable",
                message="Feature preferences could not be verified for this Workflow.",
                denied_by="feature_preferences",
                details={
                    "disabled": True,
                    "requires": list(required_components),
                },
            ))
        elif preferences.opted_out:
            reasons.append(WorkflowAdmissionReason(
                code="feature_opted_out",
                message=(
                    "Workflow execution is disabled by current feature preferences: "
                    + ", ".join(preferences.opted_out)
                ),
                denied_by="feature_preferences",
                details={
                    "disabled": True,
                    "opted_out": list(preferences.opted_out),
                    "requires": list(required_components),
                },
            ))

        unknown_components: list[str] = []
        if invocation_context.component_availability is None:
            missing_components: list[str] = []
            for component in required_components:
                try:
                    available = self._component_provider(component)
                except Exception:
                    unknown_components.append(component)
                    continue
                if not available:
                    missing_components.append(component)
        else:
            missing_components = [
                component
                for component in required_components
                if invocation_context.component_availability.get(component) is not True
            ]
        if missing_components:
            reasons.append(WorkflowAdmissionReason(
                code="component_unavailable",
                message=(
                    "Workflow dependencies are unavailable: "
                    + ", ".join(missing_components)
                ),
                denied_by="components",
                details={
                    "disabled": True,
                    "missing_components": missing_components,
                    "requires": list(required_components),
                },
            ))
        if unknown_components:
            reasons.append(WorkflowAdmissionReason(
                code="component_availability_unknown",
                message=(
                    "Workflow dependency availability could not be verified: "
                    + ", ".join(unknown_components)
                ),
                denied_by="components",
                details={
                    "disabled": True,
                    "unknown_components": unknown_components,
                    "requires": list(required_components),
                },
            ))

        missing_executors: list[dict[str, Any]] = []
        for step in getattr(workflow, "steps", ()) or ():
            alternatives = _executor_requirement(step)
            if alternatives and not any(
                facility in invocation_context.executor_facilities
                for facility in alternatives
            ):
                missing_executors.append({
                    "step_id": getattr(step, "id", None),
                    "required_any": [facility.value for facility in alternatives],
                })
        if missing_executors:
            reasons.append(WorkflowAdmissionReason(
                code="executor_unavailable",
                message="The invocation environment cannot execute every Workflow Step.",
                denied_by="executor_facilities",
                details={
                    "available_executor_facilities": sorted(
                        facility.value for facility in invocation_context.executor_facilities
                    ),
                    "missing_executor_requirements": missing_executors,
                },
            ))

        return WorkflowAdmissionDecision(tuple(reasons))

    def evaluate_address(
        self,
        workflow_name: str,
        invocation_context: WorkflowInvocationContext,
    ) -> WorkflowAdmissionDecision:
        """Evaluate rules that must run before registry resolution.

        Authorization is intentionally address-first so an ACL-scoped caller
        cannot use error differences as a registry existence oracle.
        """

        reason = self._authorization_reason(workflow_name, invocation_context)
        return WorkflowAdmissionDecision(() if reason is None else (reason,))

    @staticmethod
    def _authorization_reason(
        workflow_name: str,
        invocation_context: WorkflowInvocationContext,
    ) -> WorkflowAdmissionReason | None:
        authorization = invocation_context.authorization
        if authorization.allowed:
            return None
        details: dict[str, Any] = {}
        if authorization.allowed_sample:
            details["allowed_sample"] = list(authorization.allowed_sample)
        if authorization.hint:
            details["hint"] = authorization.hint
        return WorkflowAdmissionReason(
            code="authorization_denied",
            message=authorization.message or (
                f"Workflow {workflow_name!r} is not permitted for this session."
            ),
            denied_by=authorization.denied_by or "authorization",
            details=details,
        )


__all__ = [
    "WorkflowAdmission",
    "WorkflowAdmissionDecision",
    "WorkflowAdmissionReason",
]
