"""Least-authority boundary for detached Journal prompt workers."""

from __future__ import annotations

import asyncio
import weakref

import pytest

from work_buddy.journal_capture.execution_identity import (
    journal_prompt_generation_session_id,
    journal_prompt_request_from_session,
)
from work_buddy.mcp_server import session_acl
from work_buddy.mcp_server.tools import gateway


_REQUEST_ID = "jpgr_" + "a" * 32
_ALLOWED = frozenset(
    {
        "journal_prompt_generation_context",
        "journal_prompt_generation_complete",
    }
)


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


def test_journal_prompt_worker_identity_is_exact_and_safe() -> None:
    session_id = journal_prompt_generation_session_id(_REQUEST_ID)
    assert session_id == f"journal-prompt:{_REQUEST_ID}"
    assert journal_prompt_request_from_session(session_id) == _REQUEST_ID

    for invalid in (
        "",
        "jpgr_short",
        "jpgr_" + "A" * 32,
        "jpgr_" + "a" * 32 + ":suffix",
        "../jpgr_" + "a" * 32,
    ):
        with pytest.raises(ValueError):
            journal_prompt_generation_session_id(invalid)
    for forged in (
        "journal-prompt:",
        "journal-prompt:jpgr_short",
        "journal-prompt:jpgr_" + "A" * 32,
        "journal-prompt:jpgr_" + "a" * 32 + ":suffix",
        "prefix:journal-prompt:" + _REQUEST_ID,
    ):
        assert journal_prompt_request_from_session(forged) is None


def test_journal_prompt_worker_has_non_overridable_builtin_acl() -> None:
    session_id = journal_prompt_generation_session_id(_REQUEST_ID)
    assert session_acl.get_session_acl(session_id) == _ALLOWED
    for forbidden in (
        "task_create",
        "agent_docs",
        "knowledge",
        "web_search",
        "conversation_send",
        "cowork_doc_get",
        "wb_init",
    ):
        assert not session_acl.is_capability_allowed(session_id, forbidden)

    session_acl.set_session_acl(
        session_id,
        ["journal_prompt_generation_context", "task_create"],
    )
    try:
        assert session_acl.get_session_acl(session_id) == frozenset(
            {"journal_prompt_generation_context"}
        )
    finally:
        session_acl.clear_session_acl(session_id)
    assert session_acl.get_session_acl(session_id) == _ALLOWED


def test_gateway_search_filters_and_run_rejects_other_capabilities(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gateway, "_SESSION_REGISTRY", weakref.WeakKeyDictionary())
    monkeypatch.setattr(gateway, "ensure_listeners_registered", lambda: None)
    monkeypatch.setattr(
        gateway.registry,
        "search_registry",
        lambda *_args, **_kwargs: [
            {"name": "journal_prompt_generation_context", "description": "context"},
            {"name": "task_create", "description": "task"},
            {"name": "journal_prompt_generation_complete", "description": "complete"},
            {"name": "agent_docs", "description": "docs"},
        ],
    )
    monkeypatch.setattr(
        gateway.registry,
        "filter_results_by_modes",
        lambda results, *_args, **_kwargs: results,
    )
    from work_buddy.mcp_server import activity_ledger

    monkeypatch.setattr(activity_ledger, "record_search", lambda *_args: None)
    mcp = _FakeMCP()
    gateway.register_tools(mcp)
    session = _FakeSession()
    context = _FakeContext(session)
    session_id = journal_prompt_generation_session_id(_REQUEST_ID)
    gateway._SESSION_REGISTRY[session] = session_id

    found = asyncio.run(mcp.tools["wb_search"]("journal", ctx=context))
    assert found["_acl_filtered"] is True
    assert {item["name"] for item in found["results"]} == _ALLOWED
    assert found["_acl_hidden_count"] == 2

    for capability in ("task_create", "agent_docs", "conversation_send"):
        denied = asyncio.run(
            mcp.tools["wb_run"](capability, params={}, ctx=context)
        )
        assert denied["denied_by"] == "session_acl"
        assert denied["allowed_sample"] == sorted(_ALLOWED)
