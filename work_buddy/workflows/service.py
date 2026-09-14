"""Application service through which driving adapters operate Workflows."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from work_buddy.workflows.admission import WorkflowAdmission
from work_buddy.workflows.context import WorkflowInvocationContext


ConsentCoordinator = Callable[[Any, WorkflowInvocationContext], Mapping[str, Any] | None]
AdmissionHook = Callable[[Any, WorkflowInvocationContext], Mapping[str, Any] | None]


class WorkflowService:
    """Transport-neutral façade over the current conductor implementation."""

    def __init__(self, *, admission: WorkflowAdmission | None = None) -> None:
        self.admission = admission or WorkflowAdmission()

    @staticmethod
    def resolve(workflow: str) -> Any | None:
        from work_buddy.mcp_server.registry import get_entry

        return get_entry(workflow)

    def invoke(
        self,
        workflow: str,
        *,
        invocation_context: WorkflowInvocationContext,
        params: dict[str, Any] | None = None,
        on_admitted: AdmissionHook | None = None,
        consent: ConsentCoordinator | None = None,
        headless: bool = False,
    ) -> dict[str, Any]:
        """Resolve, admit, optionally coordinate consent, and start a run."""

        address_decision = self.admission.evaluate_address(workflow, invocation_context)
        if not address_decision.allowed:
            return address_decision.denial_payload(_Address(workflow))

        entry = self.resolve(workflow)
        if entry is None:
            return {"error": f"Unknown workflow: {workflow!r}"}
        if not hasattr(entry, "steps") or hasattr(entry, "callable"):
            return {
                "error": f"{workflow!r} is a direct Skill, not a Workflow.",
                "error_code": "not_a_workflow",
            }

        decision = self.admission.evaluate(entry, invocation_context)
        if not decision.allowed:
            return decision.denial_payload(entry)

        from work_buddy.mcp_server.conductor import validate_workflow_params

        params_valid, params_error = validate_workflow_params(entry, params)
        if not params_valid:
            return {
                "error": params_error,
                "error_code": "workflow_params_invalid",
                "workflow_id": getattr(entry, "workflow_id", None),
                "workflow_name": getattr(entry, "name", workflow),
            }

        if on_admitted is not None:
            hook_result = on_admitted(entry, invocation_context)
            if hook_result is not None:
                return dict(hook_result)

        if consent is not None:
            consent_result = consent(entry, invocation_context)
            if consent_result is not None:
                return dict(consent_result)

        from work_buddy.mcp_server.conductor import start_workflow

        internal_target = getattr(entry, "workflow_id", "") or getattr(entry, "name", workflow)
        return start_workflow(
            internal_target,
            params=params,
            agent_session_id=invocation_context.session_id,
            headless=headless,
            expected_revision=getattr(entry, "workflow_revision", None),
        )

    @staticmethod
    def advance(
        workflow_run_id: str,
        step_result: Any | None = None,
        *,
        agent_session_id: str | None = None,
    ) -> dict[str, Any]:
        from work_buddy.mcp_server.conductor import advance_workflow

        return advance_workflow(workflow_run_id, step_result, agent_session_id)

    @staticmethod
    def cancel(workflow_run_id: str, reason: str | None = None) -> dict[str, Any]:
        from work_buddy.mcp_server.conductor import cancel_workflow

        return cancel_workflow(workflow_run_id, reason)

    @staticmethod
    def status(workflow_run_id: str) -> dict[str, Any]:
        from work_buddy.mcp_server.conductor import get_workflow_status

        return get_workflow_status(workflow_run_id)

    @staticmethod
    def get_step_result(
        workflow_run_id: str,
        step_id: str,
        key: str | None = None,
    ) -> dict[str, Any]:
        from work_buddy.mcp_server.conductor import get_step_result

        return get_step_result(workflow_run_id, step_id, key)


_DEFAULT_SERVICE: WorkflowService | None = None
_DEFAULT_SERVICE_LOCK = threading.Lock()


def get_workflow_service() -> WorkflowService:
    """Return the process-local Workflow application service."""

    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        with _DEFAULT_SERVICE_LOCK:
            if _DEFAULT_SERVICE is None:
                _DEFAULT_SERVICE = WorkflowService()
    return _DEFAULT_SERVICE


class _Address:
    """Non-resolving identity used for address-first denial payloads."""

    workflow_id = None

    def __init__(self, name: str) -> None:
        self.name = name


__all__ = [
    "AdmissionHook",
    "ConsentCoordinator",
    "WorkflowService",
    "get_workflow_service",
]
