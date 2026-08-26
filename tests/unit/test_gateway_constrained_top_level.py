"""Constrained MCP sessions cannot bypass their capability ACL."""

from __future__ import annotations

import asyncio
import weakref

import pytest

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
            "capability": "wb_init",
            "params": {"session_id": "forged-unconstrained-session"},
        }
    )

    result = asyncio.run(
        registered_gateway.tools[tool_name](**kwargs, ctx=context)
    )

    assert result["denied_by"] == "session_acl"
    assert gateway._SESSION_REGISTRY[session] == bound_id


def test_capability_cannot_replace_its_transport_owned_form_agent_identity():
    def operation(*, agent_session_id):
        return agent_session_id

    bound_id = "generation-transport-assisted-draft"
    assert gateway._invoke_with_session(
        operation, bound_id, agent_session_id="forged-unconstrained-session",
    ) == bound_id
