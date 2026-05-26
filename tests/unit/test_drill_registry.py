"""Tests for `work_buddy.summarization.drill_registry`.

Registry-level behavior: register / lookup / reset / default registrations.
The `summary` source handler's namespace-dispatch behavior is exercised by
`test_summarization_funnel.py` since that's its concrete use-site.
"""

from __future__ import annotations

import pytest

from work_buddy.summarization.drill_registry import (
    _register_defaults,
    _reset_for_tests,
    available_sources,
    get_drill_handler,
    register_drill_handler,
)


@pytest.fixture(autouse=True)
def _restore_defaults_after_test():
    """Every test that touches the registry must leave it in the
    default-registered state so test order doesn't matter."""
    yield
    _reset_for_tests()
    _register_defaults()


def test_default_registry_contains_summary():
    assert "summary" in available_sources()
    handler = get_drill_handler("summary")
    assert handler is not None
    assert callable(handler)


def test_unknown_source_returns_none():
    assert get_drill_handler("nonsense_source_xyz") is None


def test_register_drill_handler_stores_handler():
    def _fake(ns, iid, query, method, top_k):
        return {"called": True}

    register_drill_handler("custom_source", _fake)
    assert "custom_source" in available_sources()
    assert get_drill_handler("custom_source") is _fake
    out = get_drill_handler("custom_source")("ns", "id", "q", "k,s", 5)
    assert out == {"called": True}


def test_register_drill_handler_is_idempotent_by_name():
    """Re-registering the same source overwrites the previous handler."""

    def _first(ns, iid, query, method, top_k):
        return "first"

    def _second(ns, iid, query, method, top_k):
        return "second"

    register_drill_handler("rotating_source", _first)
    register_drill_handler("rotating_source", _second)
    result = get_drill_handler("rotating_source")("n", "i", "q", "k", 5)
    assert result == "second"


def test_reset_for_tests_clears_registry():
    register_drill_handler("ephemeral", lambda *a, **k: None)
    assert "ephemeral" in available_sources()
    _reset_for_tests()
    assert available_sources() == []


def test_register_defaults_restores_summary_handler():
    _reset_for_tests()
    assert get_drill_handler("summary") is None
    _register_defaults()
    assert get_drill_handler("summary") is not None


def test_summary_handler_dispatches_conversation_session(monkeypatch):
    """The registered `summary`-source handler routes
    conversation_session to session_search via the namespace dispatcher."""
    from work_buddy.sessions import inspector

    calls: list[dict] = []

    def fake_session_search(
        session_id, query, method="keyword,semantic", top_k=5,
    ):
        calls.append({
            "session_id": session_id, "query": query,
            "method": method, "top_k": top_k,
        })
        return {"hits": []}

    monkeypatch.setattr(inspector, "session_search", fake_session_search)

    handler = get_drill_handler("summary")
    result = handler(
        "conversation_session", "sess-x", "the query",
        "keyword,semantic", 7,
    )
    assert calls == [{
        "session_id": "sess-x", "query": "the query",
        "method": "keyword,semantic", "top_k": 7,
    }]
    assert result == {"hits": []}


def test_summary_handler_returns_none_for_unknown_namespace():
    handler = get_drill_handler("summary")
    result = handler(
        "unknown_namespace", "id", "q", "keyword", 5,
    )
    assert result is None
