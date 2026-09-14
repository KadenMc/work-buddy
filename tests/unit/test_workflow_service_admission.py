"""Focused contract tests for Workflow admission and the application service.

These tests deliberately construct transport-neutral invocation facts.  They
exercise policy independently from the MCP gateway and the sidecar so both
adapters are required to get the same answer for the same facts.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import pytest

from work_buddy.mcp_server.registry import (
    AutoRun,
    WorkflowDefinition,
    WorkflowStep,
)
from work_buddy.workflows.admission import WorkflowAdmission
from work_buddy.workflows.context import (
    AuthorizationContext,
    ExecutorFacility,
    FeaturePreferenceContext,
    InteractionAttendance,
    InvocationChannel,
    InvocationContext,
    WorkflowEntrySurface,
    WorkflowInvocationContext,
)
from work_buddy.workflows.service import WorkflowService


_DEFAULT_COMPONENTS = object()


def _workflow(
    *,
    steps: list[WorkflowStep] | None = None,
    **overrides: Any,
) -> WorkflowDefinition:
    values: dict[str, Any] = {
        "name": "test-workflow",
        "description": "Workflow admission test fixture",
        "workflow_file": "store:test/test-workflow",
        "execution": "main",
        "workflow_id": "wfd_11111111111111111111111111111111",
        "workflow_revision": "revision-1",
        "steps": [] if steps is None else steps,
    }
    values.update(overrides)
    return WorkflowDefinition(**values)


def _context(
    *,
    channel: InvocationChannel = InvocationChannel.INTERNAL,
    entry_surface: WorkflowEntrySurface = WorkflowEntrySurface.INTERNAL,
    invocation_context: InvocationContext = InvocationContext.USER_INVOCATION,
    active_modes: frozenset[str] = frozenset(),
    attendance: InteractionAttendance = InteractionAttendance.ATTENDED,
    executor_facilities: frozenset[ExecutorFacility] = frozenset(ExecutorFacility),
    feature_preferences: FeaturePreferenceContext | None = FeaturePreferenceContext(),
    component_availability: object = _DEFAULT_COMPONENTS,
    authorization: AuthorizationContext = AuthorizationContext(),
    skill_surface_eligible: bool | None = None,
    session_id: str | None = "test-session",
) -> WorkflowInvocationContext:
    components = {} if component_availability is _DEFAULT_COMPONENTS else component_availability
    return WorkflowInvocationContext(
        channel=channel,
        entry_surface=entry_surface,
        invocation_context=invocation_context,
        session_id=session_id,
        active_modes=active_modes,
        attendance=attendance,
        executor_facilities=executor_facilities,
        feature_preferences=feature_preferences,
        component_availability=components,  # type: ignore[arg-type]
        authorization=authorization,
        skill_surface_eligible=skill_surface_eligible,
    )


def _step(
    *,
    step_type: str = "reasoning",
    execution: str = "main",
    auto_run: AutoRun | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        id="step-1",
        name="Step 1",
        instruction="Do the bounded test step.",
        step_type=step_type,
        execution=execution,
        auto_run=auto_run,
    )


def test_admission_returns_every_structured_reason_in_policy_order() -> None:
    workflow = _workflow(
        available_when="dev",
        available_in={InvocationContext.AGENT_AUTONOMOUS},
        requires=["obsidian"],
        steps=[_step()],
    )
    context = _context(
        entry_surface=WorkflowEntrySurface.SKILL_GATEWAY,
        executor_facilities=frozenset(),
        feature_preferences=FeaturePreferenceContext(available=False),
        component_availability={"obsidian": False},
        authorization=AuthorizationContext(
            allowed=False,
            denied_by="session_acl",
            message="Not permitted by the session ACL.",
            allowed_sample=("allowed-workflow",),
            hint="Use an authorized session.",
        ),
        skill_surface_eligible=False,
    )

    decision = WorkflowAdmission().evaluate(workflow, context)

    assert decision.allowed is False
    assert [reason.code for reason in decision.reasons] == [
        "authorization_denied",
        "skill_surface_ineligible",
        "mode_not_active",
        "invocation_context_denied",
        "feature_preference_unavailable",
        "component_unavailable",
        "executor_unavailable",
    ]
    assert [reason.denied_by for reason in decision.reasons] == [
        "session_acl",
        "skill_surface",
        "mode_gate",
        "invocation_context",
        "feature_preferences",
        "components",
        "executor_facilities",
    ]
    payload = decision.denial_payload(workflow)
    assert payload["error_code"] == "authorization_denied"
    assert payload["workflow_id"] == workflow.workflow_id
    assert payload["admission"] == decision.to_dict()
    assert payload["allowed_sample"] == ["allowed-workflow"]
    assert payload["hint"] == "Use an authorized session."


@pytest.mark.parametrize(
    ("active_modes", "allowed"),
    [
        (frozenset({"dev", "knowledge"}), True),
        (frozenset({"dev"}), False),
        (frozenset({"knowledge"}), False),
    ],
)
def test_mode_gate_is_composed_from_active_mode_facts(
    active_modes: frozenset[str],
    allowed: bool,
) -> None:
    workflow = _workflow(available_when="dev & knowledge")

    decision = WorkflowAdmission().evaluate(
        workflow,
        _context(active_modes=active_modes),
    )

    assert decision.allowed is allowed
    if not allowed:
        reason = decision.reasons[0]
        assert reason.code == "mode_not_active"
        assert reason.details == {
            "disabled": True,
            "required_modes": ["dev", "knowledge"],
            "active_modes": sorted(active_modes),
        }


class _ReloadedInvocationContext(str, Enum):
    """Value-compatible stand-in for an enum class recreated by reload."""

    USER_INVOCATION = "user_invocation"


@pytest.mark.parametrize(
    "available_in",
    [
        {InvocationContext.USER_INVOCATION},
        {_ReloadedInvocationContext.USER_INVOCATION},
        {"user_invocation"},
    ],
)
def test_available_in_compares_semantic_values_not_enum_identity(
    available_in: set[Any],
) -> None:
    workflow = _workflow(available_in=available_in)

    assert WorkflowAdmission().evaluate(workflow, _context()).allowed is True


def test_available_in_denial_reports_active_and_allowed_contexts() -> None:
    workflow = _workflow(available_in={InvocationContext.AGENT_AUTONOMOUS})

    decision = WorkflowAdmission().evaluate(workflow, _context())

    assert decision.allowed is False
    assert decision.reasons[0].code == "invocation_context_denied"
    assert decision.reasons[0].details == {
        "invocation_context": "user_invocation",
        "available_in": ["agent_autonomous"],
    }


@pytest.mark.parametrize(
    ("step", "facilities", "allowed", "required"),
    [
        (
            _step(
                step_type="code",
                execution="main",
                auto_run=AutoRun(callable="work_buddy.example.run"),
            ),
            frozenset({ExecutorFacility.PROGRAM, ExecutorFacility.SUBAGENT}),
            True,
            None,
        ),
        (
            _step(step_type="reasoning", execution="subagent"),
            frozenset({ExecutorFacility.PROGRAM, ExecutorFacility.SUBAGENT}),
            True,
            None,
        ),
        (
            _step(step_type="code", execution="main"),
            frozenset({ExecutorFacility.PROGRAM, ExecutorFacility.SUBAGENT}),
            False,
            "calling_agent",
        ),
        (
            _step(step_type="code", execution="main"),
            frozenset({ExecutorFacility.CALLING_AGENT}),
            True,
            None,
        ),
    ],
)
def test_scheduler_and_interactive_facilities_admit_only_executable_steps(
    step: WorkflowStep,
    facilities: frozenset[ExecutorFacility],
    allowed: bool,
    required: str | None,
) -> None:
    is_scheduler = ExecutorFacility.SUBAGENT in facilities
    context = _context(
        channel=InvocationChannel.SIDECAR if is_scheduler else InvocationChannel.MCP,
        entry_surface=(
            WorkflowEntrySurface.SCHEDULER
            if is_scheduler
            else WorkflowEntrySurface.SKILL_GATEWAY
        ),
        invocation_context=(
            InvocationContext.AGENT_AUTONOMOUS
            if is_scheduler
            else InvocationContext.USER_INVOCATION
        ),
        attendance=(
            InteractionAttendance.UNATTENDED
            if is_scheduler
            else InteractionAttendance.ATTENDED
        ),
        executor_facilities=facilities,
    )
    workflow = _workflow(steps=[step])

    decision = WorkflowAdmission().evaluate(workflow, context)

    assert decision.allowed is allowed
    if required is not None:
        assert decision.reasons[0].code == "executor_unavailable"
        assert decision.reasons[0].details["missing_executor_requirements"] == [
            {"step_id": "step-1", "required_any": [required]},
        ]


def test_compiled_required_components_exclude_optional_only_dependencies() -> None:
    workflow = _workflow(
        requires=["mandatory-component", "optional-only-component"],
        required_components=["mandatory-component"],
    )

    decision = WorkflowAdmission().evaluate(
        workflow,
        _context(component_availability={"mandatory-component": True}),
    )

    assert decision.allowed is True


def test_explicit_component_snapshot_denies_missing_dependency() -> None:
    workflow = _workflow(requires=["obsidian"])

    decision = WorkflowAdmission().evaluate(
        workflow,
        _context(component_availability={"obsidian": False}),
    )

    assert decision.allowed is False
    assert decision.reasons[0].code == "component_unavailable"
    assert decision.reasons[0].details["missing_components"] == ["obsidian"]


def test_component_provider_exception_is_unknown_not_available() -> None:
    workflow = _workflow(requires=["obsidian"])

    def unavailable_provider(_component: str) -> bool:
        raise RuntimeError("probe failed")

    decision = WorkflowAdmission(
        component_provider=unavailable_provider,
    ).evaluate(
        workflow,
        _context(component_availability=None),
    )

    assert decision.allowed is False
    assert decision.reasons[0].code == "component_availability_unknown"
    assert decision.reasons[0].details["unknown_components"] == ["obsidian"]


def test_default_component_provider_recovers_from_stale_negative_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy import recovery
    from work_buddy import tools

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tools,
        "is_tool_available",
        lambda component: calls.append(("cached", component)) or False,
    )
    monkeypatch.setattr(
        recovery,
        "recheck_tool",
        lambda component: calls.append(("recheck", component)) or True,
    )
    workflow = _workflow(requires=["obsidian"])

    decision = WorkflowAdmission().evaluate(
        workflow,
        _context(component_availability=None),
    )

    assert decision.allowed is True
    assert calls == [("cached", "obsidian"), ("recheck", "obsidian")]


@pytest.mark.parametrize(
    ("preferences", "expected_code"),
    [
        (
            FeaturePreferenceContext(available=False),
            "feature_preference_unavailable",
        ),
        (
            FeaturePreferenceContext(available=True, opted_out=("obsidian",)),
            "feature_opted_out",
        ),
    ],
)
def test_feature_preference_failures_are_distinct_structured_denials(
    preferences: FeaturePreferenceContext,
    expected_code: str,
) -> None:
    decision = WorkflowAdmission().evaluate(
        _workflow(requires=["obsidian"]),
        _context(
            feature_preferences=preferences,
            component_availability={"obsidian": True},
        ),
    )

    assert decision.allowed is False
    assert decision.reasons[0].code == expected_code
    assert decision.reasons[0].denied_by == "feature_preferences"


def test_acl_denial_occurs_before_resolution_and_does_not_leak_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkflowService()

    def should_not_resolve(_workflow: str) -> Any:
        pytest.fail("ACL denial must happen before registry resolution")

    monkeypatch.setattr(service, "resolve", should_not_resolve)
    context = _context(
        authorization=AuthorizationContext(
            allowed=False,
            denied_by="session_acl",
            message="That address is outside this session's ACL.",
        ),
    )

    result = service.invoke("possibly-secret-workflow", invocation_context=context)

    assert result["error_code"] == "authorization_denied"
    assert result["denied_by"] == "session_acl"
    assert result["workflow_id"] is None
    assert result["workflow_name"] == "possibly-secret-workflow"


def test_service_orders_admission_hook_consent_and_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.mcp_server import conductor

    events: list[str] = []

    class RecordingAdmission(WorkflowAdmission):
        def evaluate_address(self, workflow_name, invocation_context):  # type: ignore[no-untyped-def]
            events.append("address-admission")
            return super().evaluate_address(workflow_name, invocation_context)

        def evaluate(self, workflow, invocation_context):  # type: ignore[no-untyped-def]
            events.append("definition-admission")
            return super().evaluate(workflow, invocation_context)

    workflow = _workflow()
    service = WorkflowService(admission=RecordingAdmission())
    monkeypatch.setattr(service, "resolve", lambda _address: workflow)

    def on_admitted(_workflow, _context):  # type: ignore[no-untyped-def]
        events.append("on-admitted")
        return None

    def consent(_workflow, _context):  # type: ignore[no-untyped-def]
        events.append("consent")
        return None

    def start_workflow(target, **kwargs):  # type: ignore[no-untyped-def]
        events.append("start")
        assert target == workflow.workflow_id
        assert kwargs == {
            "params": None,
            "agent_session_id": "test-session",
            "headless": True,
            "expected_revision": "revision-1",
        }
        return {"workflow_run_id": "wf_run_1"}

    monkeypatch.setattr(conductor, "start_workflow", start_workflow)

    result = service.invoke(
        "human-readable-alias",
        invocation_context=_context(),
        on_admitted=on_admitted,
        consent=consent,
        headless=True,
    )

    assert result == {"workflow_run_id": "wf_run_1"}
    assert events == [
        "address-admission",
        "definition-admission",
        "on-admitted",
        "consent",
        "start",
    ]


def test_parameter_validation_precedes_hooks_and_conductor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.mcp_server import conductor

    workflow = _workflow(
        params_schema={"project": {"type": "str", "required": True}},
    )
    service = WorkflowService()
    monkeypatch.setattr(service, "resolve", lambda _address: workflow)
    events: list[str] = []

    monkeypatch.setattr(
        conductor,
        "start_workflow",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid params must not reach the conductor"
        ),
    )

    result = service.invoke(
        workflow.name,
        invocation_context=_context(),
        params={},
        on_admitted=lambda *_args: events.append("on-admitted"),
        consent=lambda *_args: events.append("consent"),
    )

    assert result["error_code"] == "workflow_params_invalid"
    assert "Missing required" in result["error"]
    assert events == []


def test_revision_guard_rejects_definition_changed_after_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.mcp_server import conductor

    admitted = _workflow(workflow_revision="revision-before")
    changed = _workflow(workflow_revision="revision-after")
    service = WorkflowService()
    monkeypatch.setattr(service, "resolve", lambda _address: admitted)
    monkeypatch.setattr(conductor, "get_entry", lambda _address: changed)

    result = service.invoke(admitted.name, invocation_context=_context())

    assert result["error_code"] == "workflow_revision_changed"
    assert result["workflow_id"] == admitted.workflow_id
    assert result["expected_workflow_revision"] == "revision-before"
    assert result["workflow_revision"] == "revision-after"


def test_lifecycle_methods_delegate_to_the_conductor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.mcp_server import conductor

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        conductor,
        "advance_workflow",
        lambda *args: calls.append(("advance", *args)) or {"advanced": True},
    )
    monkeypatch.setattr(
        conductor,
        "cancel_workflow",
        lambda *args: calls.append(("cancel", *args)) or {"cancelled": True},
    )
    monkeypatch.setattr(
        conductor,
        "get_workflow_status",
        lambda *args: calls.append(("status", *args)) or {"status": "running"},
    )
    monkeypatch.setattr(
        conductor,
        "get_step_result",
        lambda *args: calls.append(("result", *args)) or {"value": 42},
    )

    assert WorkflowService.advance("wf_run", {"ok": True}, agent_session_id="session") == {
        "advanced": True,
    }
    assert WorkflowService.cancel("wf_run", "because") == {"cancelled": True}
    assert WorkflowService.status("wf_run") == {"status": "running"}
    assert WorkflowService.get_step_result("wf_run", "step", "field") == {"value": 42}
    assert calls == [
        ("advance", "wf_run", {"ok": True}, "session"),
        ("cancel", "wf_run", "because"),
        ("status", "wf_run"),
        ("result", "wf_run", "step", "field"),
    ]
