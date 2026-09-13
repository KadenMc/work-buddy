"""Constrained MCP sessions cannot bypass their skill ACL."""

from __future__ import annotations

import asyncio
import json
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.mcp_server.registry import Skill
from work_buddy.mcp_server.tools import gateway


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class _FakeSession:
    pass


class _FakeContext:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session


@pytest.fixture
def registered_gateway(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_SESSION_REGISTRY",
        weakref.WeakKeyDictionary(),
    )
    monkeypatch.setattr(gateway, "ensure_listeners_registered", lambda: None)
    mcp = _FakeMCP()
    gateway.register_tools(mcp)
    return mcp


@pytest.mark.parametrize(
    "session_id",
    ["generation-123-cowork", "generation-123-assisted-draft"],
)
@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("wb_advance", ("wf_other",)),
        ("wb_status", ()),
        ("wb_step_result", ("wf_other", "step-a")),
        ("wb_skill_result", ("op_other",)),
        # Legacy top-level alias must enforce the same ACL.
        ("wb_capability_result", ("op_other",)),
    ],
)
def test_hosted_session_cannot_call_top_level_acl_bypasses(
    registered_gateway,
    tool_name,
    args,
    session_id,
) -> None:
    session = _FakeSession()
    context = _FakeContext(session)
    gateway._SESSION_REGISTRY[session] = session_id

    result = asyncio.run(
        registered_gateway.tools[tool_name](*args, ctx=context)
    )

    assert result == {
        "error": (
            f"{tool_name} is not permitted for this constrained "
            "execution session."
        ),
        "denied_by": "session_acl",
    }


@pytest.mark.parametrize("tool_name", ["wb_init", "wb_run"])
def test_form_agent_cannot_rebind_transport_to_an_unconstrained_session(
    registered_gateway, tool_name,
) -> None:
    session = _FakeSession()
    context = _FakeContext(session)
    bound_id = "generation-123-assisted-draft"
    gateway._SESSION_REGISTRY[session] = bound_id
    kwargs = (
        {"session_id": "forged-unconstrained-session"}
        if tool_name == "wb_init"
        else {
            "skill": "wb_init",
            "params": {"session_id": "forged-unconstrained-session"},
        }
    )

    result = asyncio.run(
        registered_gateway.tools[tool_name](**kwargs, ctx=context)
    )

    assert result["denied_by"] == "session_acl"
    assert gateway._SESSION_REGISTRY[session] == bound_id


def test_skill_cannot_replace_its_transport_owned_form_agent_identity():
    def operation(*, agent_session_id):
        return agent_session_id

    bound_id = "generation-transport-assisted-draft"
    assert gateway._invoke_with_session(
        operation, bound_id, agent_session_id="forged-unconstrained-session",
    ) == bound_id


def test_wb_run_accepts_deprecated_capability_argument(registered_gateway):
    """Cached pre-migration tool schemas remain usable at the MCP boundary."""
    session = _FakeSession()
    context = _FakeContext(session)

    result = asyncio.run(registered_gateway.tools["wb_run"](
        capability="wb_init",
        params={"session_id": "legacy-schema-client"},
        ctx=context,
    ))

    assert result["status"] == "initialized"
    assert result["session_id"] == "legacy-schema-client"


def test_wb_run_rejects_conflicting_skill_and_legacy_alias(registered_gateway):
    result = asyncio.run(registered_gateway.tools["wb_run"](
        skill="task_read",
        capability="task_toggle",
        params={},
    ))

    assert "conflicting values" in result["error"]


def test_wb_run_does_not_replace_explicit_empty_skill_with_legacy_alias(
    registered_gateway,
):
    result = asyncio.run(registered_gateway.tools["wb_run"](
        skill="",
        capability="task_read",
        params={},
    ))

    assert "conflicting values" in result["error"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"capability_name": "legacy"}, "legacy"),
        (
            {
                "skill_name": "canonical",
                "capability_name": "legacy_should_not_override",
            },
            "canonical",
        ),
    ],
)
def test_skill_param_alias_is_consumed_before_persistence_and_dispatch(
    registered_gateway, monkeypatch, tmp_path, params, expected,
):
    session = _FakeSession()
    context = _FakeContext(session)
    gateway._SESSION_REGISTRY[session] = "alias-precedence-session"
    received = {}

    def operation(*, skill_name):
        received["skill_name"] = skill_name
        return {"skill_name": skill_name}

    entry = Skill(
        name="alias_precedence_probe",
        description="Probe canonical parameter precedence.",
        category="status",
        parameters={"skill_name": {"type": "str", "required": False}},
        callable=operation,
        param_aliases={"capability_name": "skill_name"},
    )
    monkeypatch.setattr(gateway.registry, "get_entry", lambda _name: entry)
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "work_buddy.mcp_server.runtime_admission.evaluate_runtime_admission",
        lambda _entry: SimpleNamespace(preference_available=True, opted_out=()),
    )
    monkeypatch.setattr(
        "work_buddy.mcp_server.activity_ledger.record_skill",
        lambda *_args, **_kwargs: None,
    )

    result = asyncio.run(registered_gateway.tools["wb_run"](
        skill="alias_precedence_probe",
        params=params,
        ctx=context,
    ))

    assert received == {"skill_name": expected}
    assert result["result"]["skill_name"] == expected
    operation_path = next(tmp_path.glob("op_*.json"))
    stored = json.loads(operation_path.read_text(encoding="utf-8"))
    assert stored["params"] == {"skill_name": expected}


def test_cached_user_job_create_schema_writes_only_canonical_job_fields(
    registered_gateway, monkeypatch, tmp_path,
):
    from work_buddy.knowledge.file_store import read_unit
    from work_buddy.mcp_server.ops.sidecar_ops import user_job_create

    store_dir = Path(__file__).resolve().parents[2] / "knowledge" / "store"
    declaration = read_unit(store_dir, "status/user_job_create")
    assert declaration is not None
    assert declaration["param_aliases"] == {"capability": "skill"}
    entry = Skill(
        name=declaration["skill_name"],
        description=declaration["description"],
        category=declaration["category"],
        parameters=declaration["parameters"],
        callable=user_job_create,
        param_aliases=declaration["param_aliases"],
        mutates_state=declaration["mutates_state"],
        retry_policy=declaration["retry_policy"],
    )
    session = _FakeSession()
    context = _FakeContext(session)
    gateway._SESSION_REGISTRY[session] = "cached-user-job-session"
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir()

    monkeypatch.setattr(gateway.registry, "get_entry", lambda _name: entry)
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", operations_dir)
    monkeypatch.setattr(
        "work_buddy.mcp_server.runtime_admission.evaluate_runtime_admission",
        lambda _entry: SimpleNamespace(preference_available=True, opted_out=()),
    )
    monkeypatch.setattr(
        "work_buddy.mcp_server.activity_ledger.record_skill",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "work_buddy.paths.data_dir",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    monkeypatch.setattr(
        "work_buddy.sidecar.scheduler.jobs._registry_names",
        lambda kind: ["task_read"] if kind == "skill" else [],
    )

    response = asyncio.run(registered_gateway.tools["wb_run"](
        skill="user_job_create",
        params={
            "name": "cached-mcp-job",
            "schedule": "0 9 * * *",
            "job_type": "capability",
            "capability": "task_read",
            "params": {},
        },
        ctx=context,
    ))

    assert response["type"] == "result"
    assert response["skill"] == "user_job_create"
    assert "capability" not in response
    assert response["result"]["success"] is True
    text = (tmp_path / "user_jobs" / "cached-mcp-job.md").read_text(
        encoding="utf-8"
    )
    assert "type: skill" in text
    assert "skill: task_read" in text
    assert "capability" not in text


def test_legacy_result_tool_matches_canonical_tool(
    registered_gateway, monkeypatch,
):
    session = _FakeSession()
    context = _FakeContext(session)
    gateway._SESSION_REGISTRY[session] = "normal-agent-session"
    payload = {"operation_id": "op_legacy", "result": {"ok": True}}
    monkeypatch.setattr(
        gateway, "_skill_result_payload", lambda _operation_id, _key: payload,
    )

    canonical = asyncio.run(registered_gateway.tools["wb_skill_result"](
        "op_legacy", ctx=context,
    ))
    legacy = asyncio.run(registered_gateway.tools["wb_capability_result"](
        "op_legacy", ctx=context,
    ))

    assert legacy == canonical == payload
