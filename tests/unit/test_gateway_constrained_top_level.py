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
    ("tool_name", "args"),
    [
        ("wb_advance", ("wf_other",)),
        ("wb_status", ()),
        ("wb_step_result", ("wf_other", "step-a")),
        ("wb_capability_result", ("op_other",)),
    ],
)
def test_cowork_session_cannot_call_top_level_acl_bypasses(
    registered_gateway,
    tool_name,
    args,
) -> None:
    session = _FakeSession()
    context = _FakeContext(session)
    gateway._SESSION_REGISTRY[session] = "generation-123-cowork"

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
