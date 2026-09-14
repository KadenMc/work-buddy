"""Driving adapters use one Workflow application boundary."""

from __future__ import annotations

import asyncio
import ast
import json
import weakref
from pathlib import Path

import pytest

from work_buddy.mcp_server import session_acl
from work_buddy.mcp_server.registry import WorkflowDefinition
from work_buddy.mcp_server.tools import gateway
from work_buddy.sidecar.dispatch import executor
from work_buddy.workflows.context import (
    AuthorizationContext,
    ExecutorFacility,
    InteractionAttendance,
    InvocationChannel,
    InvocationContext,
    WorkflowEntrySurface,
)
from work_buddy.workflows.service import WorkflowService


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class _FakeSession:
    pass


class _FakeContext:
    def __init__(self, session):
        self.session = session


class _ServiceStub:
    def __init__(self, start_response, advance_response=None):
        self.start_response = start_response
        self.advance_response = advance_response
        self.invoke_calls = []
        self.advance_calls = []

    def invoke(self, workflow, **kwargs):
        self.invoke_calls.append((workflow, kwargs))
        return dict(self.start_response)

    def advance(self, workflow_run_id, step_result=None, **kwargs):
        self.advance_calls.append((workflow_run_id, step_result, kwargs))
        return dict(self.advance_response or {})


def _install_service(monkeypatch, service):
    monkeypatch.setattr(
        "work_buddy.workflows.service.get_workflow_service",
        lambda: service,
    )


def _registered_gateway(monkeypatch):
    monkeypatch.setattr(gateway, "_SESSION_REGISTRY", weakref.WeakKeyDictionary())
    monkeypatch.setattr(gateway, "ensure_listeners_registered", lambda: None)
    mcp = _FakeMCP()
    gateway.register_tools(mcp)
    return mcp


def test_mcp_invokes_service_with_gateway_context_and_preserves_denial(
    monkeypatch,
    tmp_path,
):
    workflow = WorkflowDefinition(
        name="canonical-flow",
        description="MCP adapter fixture",
        workflow_file="store:test/canonical-flow",
        execution="main",
        workflow_id="wfd_11111111111111111111111111111111",
        workflow_revision="sha256:" + "2" * 64,
        aliases=("former-flow",),
    )
    denial = {
        "error": "A required executor is unavailable.",
        "error_code": "executor_unavailable",
        "denied_by": "executor_facilities",
        "workflow_id": workflow.workflow_id,
        "workflow_name": workflow.name,
        "admission": {
            "allowed": False,
            "reasons": [
                {
                    "code": "executor_unavailable",
                    "message": "A required executor is unavailable.",
                    "denied_by": "executor_facilities",
                    "details": {},
                }
            ],
        },
    }
    service = _ServiceStub(denial)
    _install_service(monkeypatch, service)
    monkeypatch.setattr(gateway.registry, "get_entry", lambda _address: workflow)
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", tmp_path)
    monkeypatch.setattr(
        gateway,
        "_workflow_authorization_context",
        lambda *_args, **_kwargs: AuthorizationContext(),
    )
    monkeypatch.setattr(
        gateway,
        "_auto_workflow_consent_request",
        lambda *_args, **_kwargs: pytest.fail(
            "definition admission denial must precede consent"
        ),
    )
    mcp = _registered_gateway(monkeypatch)
    session = _FakeSession()
    context = _FakeContext(session)
    gateway._SESSION_REGISTRY[session] = "mcp-workflow-session"

    result = asyncio.run(
        mcp.tools["wb_run"](
            skill="former-flow",
            params={"value": 1},
            ctx=context,
        )
    )

    assert result == denial
    assert len(service.invoke_calls) == 1
    address, kwargs = service.invoke_calls[0]
    invocation = kwargs["invocation_context"]
    assert address == "former-flow"
    assert kwargs["params"] == {"value": 1}
    assert invocation.channel is InvocationChannel.MCP
    assert invocation.entry_surface is WorkflowEntrySurface.SKILL_GATEWAY
    assert invocation.invocation_context is InvocationContext.AGENT_CONVERSATION
    assert invocation.attendance is InteractionAttendance.ATTENDED
    assert invocation.executor_facilities == frozenset({
        ExecutorFacility.PROGRAM,
        ExecutorFacility.CALLING_AGENT,
        ExecutorFacility.SUBAGENT,
    })
    assert invocation.session_id == "mcp-workflow-session"
    assert list(tmp_path.glob("op_*.json")) == []


def test_mcp_acl_denial_is_address_first_for_known_and_unknown_workflows(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(session_acl, "_SESSION_ACL", {})
    session_acl.set_session_acl("constrained-session", ["allowed-skill"])
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", tmp_path)
    monkeypatch.setattr(
        gateway.registry,
        "get_entry",
        lambda _address: pytest.fail(
            "ACL denial must happen before registry resolution"
        ),
    )
    service = WorkflowService()
    monkeypatch.setattr(
        service,
        "resolve",
        lambda _address: pytest.fail(
            "ACL denial must happen before service resolution"
        ),
    )
    _install_service(monkeypatch, service)
    mcp = _registered_gateway(monkeypatch)
    session = _FakeSession()
    context = _FakeContext(session)
    gateway._SESSION_REGISTRY[session] = "constrained-session"

    addresses = ("known-looking-workflow", "definitely-absent-workflow")
    results = [
        asyncio.run(mcp.tools["wb_run"](skill=address, params={}, ctx=context))
        for address in addresses
    ]

    for address, result in zip(addresses, results, strict=True):
        assert result["error_code"] == "authorization_denied"
        assert result["denied_by"] == "session_acl"
        assert result["workflow_id"] is None
        assert result["workflow_name"] == address
        assert result["admission"]["allowed"] is False
        assert result["admission"]["reasons"][0]["code"] == "authorization_denied"
    assert set(results[0]) == set(results[1])
    assert list(tmp_path.glob("op_*.json")) == []


def test_sidecar_invokes_service_with_scheduler_context(monkeypatch):
    service = _ServiceStub({
        "type": "workflow_complete",
        "workflow_run_id": "wf_12345678",
        "workflow_id": "wfd_11111111111111111111111111111111",
        "workflow_revision": "sha256:" + "2" * 64,
        "workflow_name": "canonical-flow",
    })
    _install_service(monkeypatch, service)

    result = executor._execute_workflow("old-flow-alias", {})

    assert result["status"] == "ok"
    assert result["workflow_name"] == "canonical-flow"
    assert len(service.invoke_calls) == 1
    workflow, kwargs = service.invoke_calls[0]
    context = kwargs["invocation_context"]
    assert workflow == "old-flow-alias"
    assert kwargs["headless"] is True
    assert context.channel is InvocationChannel.SIDECAR
    assert context.entry_surface is WorkflowEntrySurface.SCHEDULER
    assert context.invocation_context is InvocationContext.AGENT_AUTONOMOUS
    assert context.attendance is InteractionAttendance.UNATTENDED
    assert context.executor_facilities == frozenset({
        ExecutorFacility.PROGRAM,
        ExecutorFacility.SUBAGENT,
    })
    assert ExecutorFacility.CALLING_AGENT not in context.executor_facilities


def test_sidecar_preserves_structured_admission_denial(monkeypatch):
    service = _ServiceStub({
        "error": "No calling agent is available.",
        "error_code": "executor_unavailable",
        "denied_by": "executor_facilities",
        "missing_executor_requirements": [
            {"step_id": "review", "required_any": ["calling_agent"]},
        ],
    })
    _install_service(monkeypatch, service)

    result = executor._execute_workflow("interactive-only", {})

    assert result["status"] == "error"
    assert result["error_code"] == "executor_unavailable"
    assert result["denied_by"] == "executor_facilities"
    assert result["missing_executor_requirements"][0]["step_id"] == "review"


def test_sidecar_does_not_report_advance_failure_as_success(monkeypatch):
    service = _ServiceStub(
        {
            "type": "workflow_step",
            "workflow_run_id": "wf_12345678",
            "workflow_id": "wfd_11111111111111111111111111111111",
            "workflow_revision": "sha256:" + "2" * 64,
            "workflow_name": "failing-flow",
            "current_step": {
                "id": "program-step",
                "name": "Program step",
                "step_type": "code",
            },
        },
        {
            "error": "Step result did not match its schema.",
            "error_code": "step_result_invalid",
        },
    )
    _install_service(monkeypatch, service)
    monkeypatch.setattr(executor, "_execute_code_step", lambda *_args: {"bad": True})

    result = executor._execute_workflow("failing-flow", {})

    assert result["status"] == "error"
    assert result["error_code"] == "step_result_invalid"
    assert result["workflow_run_id"] == "wf_12345678"
    assert len(service.advance_calls) == 1


def test_workflow_operation_record_pins_definition_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", tmp_path)

    operation_id = gateway._save_operation(
        "canonical-flow",
        {"value": 1},
        "manual",
        op_type="workflow",
        workflow_id="wfd_11111111111111111111111111111111",
        workflow_revision="sha256:" + "2" * 64,
        originating_session_id="agent-session",
    )

    record = json.loads(
        (tmp_path / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert record["name"] == "canonical-flow"
    assert record["workflow_id"] == "wfd_11111111111111111111111111111111"
    assert record["workflow_revision"] == "sha256:" + "2" * 64
    assert record["originating_session_id"] == "agent-session"


def test_workflow_retry_refuses_a_changed_definition_revision(monkeypatch, tmp_path):
    workflow_id = "wfd_11111111111111111111111111111111"
    old_revision = "sha256:" + "2" * 64
    new_revision = "sha256:" + "3" * 64
    workflow = WorkflowDefinition(
        name="canonical-flow",
        description="Changed Workflow fixture",
        workflow_file="store:test/canonical-flow",
        execution="main",
        workflow_id=workflow_id,
        workflow_revision=new_revision,
    )
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", tmp_path)
    monkeypatch.setattr(gateway.registry, "get_entry", lambda _address: workflow)
    monkeypatch.setattr(
        "work_buddy.workflows.service.get_workflow_service",
        lambda: pytest.fail("a changed Workflow retry must not dispatch"),
    )
    operation_id = gateway._save_operation(
        workflow.name,
        {},
        "auto",
        op_type="workflow",
        workflow_id=workflow_id,
        workflow_revision=old_revision,
    )
    gateway._complete_operation(operation_id, error="transient failure")

    result = gateway.retry_operation(operation_id)

    assert result["error_code"] == "workflow_revision_changed"
    assert result["workflow_id"] == workflow_id
    assert result["expected_workflow_revision"] == old_revision
    assert result["workflow_revision"] == new_revision
    record = json.loads(
        (tmp_path / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "failed"


def test_driving_adapters_do_not_import_conductor_lifecycle_functions():
    repo = Path(__file__).resolve().parents[2]
    adapter_paths = (
        repo / "work_buddy/mcp_server/tools/gateway.py",
        repo / "work_buddy/sidecar/dispatch/executor.py",
        repo / "work_buddy/mcp_server/ops/workflow_ops.py",
    )
    lifecycle_names = {
        "start_workflow",
        "advance_workflow",
        "cancel_workflow",
        "get_workflow_status",
        "get_step_result",
    }
    violations = []
    for path in adapter_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "work_buddy.mcp_server.conductor"
            ):
                imported = {alias.name for alias in node.names} & lifecycle_names
                if imported:
                    violations.append((path.name, sorted(imported)))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "_conductor"
                and node.func.attr in lifecycle_names
            ):
                violations.append((path.name, [node.func.attr]))

    assert violations == []
