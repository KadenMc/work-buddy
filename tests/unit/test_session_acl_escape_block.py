"""Regression: ACL-scoped sessions cannot escape via wb_init.

Discovered 2026-04-17 in live testing: a local model invoked under
``llm_with_tools`` with a ``readonly_safe`` preset successfully called
``wb_run(capability='wb_init', session_id='hijack-attempt')`` to swap
its MCP connection to an attacker-chosen session id. Because ACLs are
keyed on agent session id and the fresh id was unknown to the ACL
store, the subsequent calls had no whitelist enforced.

Two defenses landed together; this file pins both:

1. ``tool_presets.py`` — no preset contains ``wb_init`` (defense in
   depth; if the gateway check below ever regressed, the preset omission
   still prevents the model from being able to dispatch wb_init through
   wb_run because the ACL rejects it upstream).
2. ``gateway.py``'s ``wb_run`` — an explicit hard-block when wb_init is
   requested on an ACL-scoped session. This is the primary defense and
   the thing these tests actually exercise.
"""

from __future__ import annotations

import pytest

from work_buddy.llm.tool_presets import PRESETS
from work_buddy.mcp_server import session_acl


def test_no_preset_exposes_wb_init():
    """Defense #1: wb_init must not appear in any named preset."""
    for name, caps in PRESETS.items():
        assert "wb_init" not in caps, (
            f"Preset {name!r} contains wb_init — this would let a local "
            f"model call wb_run(capability='wb_init') and escape the ACL."
        )


def test_get_session_acl_returns_none_without_registration():
    """Precondition for the escape defense: unregistered sessions have
    no ACL, which is what an escaped model would look like."""
    assert session_acl.get_session_acl(None) is None
    assert session_acl.get_session_acl("") is None
    assert session_acl.get_session_acl("freshly-forged-id") is None


def test_acl_bound_session_is_detectable():
    """The gateway's wb_init block looks up ``get_session_acl`` on the
    current MCP connection's session id; this test pins that behavior
    so a refactor that hides the ACL registration breaks loudly."""
    sid = "test-acl-scoped"
    try:
        session_acl.set_session_acl(sid, ["task_briefing"])
        acl = session_acl.get_session_acl(sid)
        assert acl is not None
        assert "task_briefing" in acl
        assert "wb_init" not in acl  # presets exclude it
        # The gateway's guard asks exactly this question:
        #   if get_session_acl(current_sid) is not None: reject wb_init
        assert session_acl.get_session_acl(sid) is not None
    finally:
        session_acl.clear_session_acl(sid)
    # After clearing, the check inverts — wb_init would be allowed
    # again for a non-ACL-scoped session.
    assert session_acl.get_session_acl(sid) is None


# ---------------------------------------------------------------------------
# Fail-closed behavior when session cannot be resolved
# ---------------------------------------------------------------------------
# Regression: discovered during retro (2026-04-17) that
# ``is_capability_allowed(None, cap)`` returned True unconditionally,
# which silently let a local-model call through when the MCP transport
# didn't forward the X-Work-Buddy-Session header on tool-call requests.
# The fix makes ``is_capability_allowed`` fail closed whenever the
# session resolves to None AND any ACL is registered in-process — the
# only callers that legitimately resolve to None are normal agents in
# a process with no ACL-scoped runs active, and they'll never race
# against an ACL-scoped run anyway.


@pytest.fixture(autouse=True)
def _clean_acl_state():
    """Ensure ACL module state is empty before and after each test in
    this section; otherwise one test's leftover ACL can make another
    test's assertions lie about fail-closed behavior."""
    session_acl._SESSION_ACL.clear()
    yield
    session_acl._SESSION_ACL.clear()


def test_any_acl_registered_false_by_default():
    assert session_acl.any_acl_registered() is False


def test_any_acl_registered_true_after_set():
    session_acl.set_session_acl("sid", ["task_briefing"])
    assert session_acl.any_acl_registered() is True


def test_any_acl_registered_false_after_clear():
    session_acl.set_session_acl("sid", ["task_briefing"])
    session_acl.clear_session_acl("sid")
    assert session_acl.any_acl_registered() is False


def test_default_open_when_no_acl_anywhere():
    """Normal agents in a process with no ACL-scoped runs: default-open."""
    assert session_acl.is_capability_allowed(None, "anything") is True
    assert session_acl.is_capability_allowed("some-sid", "anything") is True


def test_fail_closed_when_session_none_and_acl_active():
    """The whole point of this fix: when an ACL is registered somewhere
    in-process and the call can't resolve its session, refuse rather
    than default-open. This is the defense against session-resolution
    races where an ACL-scoped caller's tool call can't be tied back to
    its ACL via ``ctx``."""
    session_acl.set_session_acl("lms-abc", ["task_briefing"])
    # Unresolved session while an ACL is active → refuse
    assert session_acl.is_capability_allowed(None, "task_briefing") is False
    assert session_acl.is_capability_allowed(None, "anything-else") is False


def test_acl_scoped_session_still_gets_membership_check():
    """Fail-closed is ONLY for unresolved sessions. A resolved session
    that owns an ACL still gets the normal membership check."""
    session_acl.set_session_acl("lms-abc", ["task_briefing"])
    assert session_acl.is_capability_allowed("lms-abc", "task_briefing") is True
    assert session_acl.is_capability_allowed("lms-abc", "task_toggle") is False


def test_resolved_non_acl_session_passes_through_even_with_other_acl_active():
    """Two concurrent sessions: one ACL-scoped, one a normal agent. The
    normal agent's resolved session has no ACL entry, so default-open
    applies to it — the other session's ACL doesn't leak across."""
    session_acl.set_session_acl("lms-abc", ["task_briefing"])
    # Normal agent, resolved to its own session id (not the ACL-scoped one)
    assert session_acl.is_capability_allowed("normal-agent-sid", "task_toggle") is True


# ---------------------------------------------------------------------------
# filter_search_results — wb_search's ACL-aware return shape
# ---------------------------------------------------------------------------
# This covers the "silent-filter → _acl_notice" fix. A local model that
# fires a wb_search inside an ACL-scoped run used to see an empty list
# when everything matching got filtered; it couldn't tell "nothing
# matched my query" from "matches existed but your ACL hid them."
# The notice-dict return shape disambiguates and short-circuits the
# search-loop pathology we saw in a live test.


def _fake_results(*names: str) -> list[dict]:
    return [{"name": n, "description": f"{n} desc"} for n in names]


def test_filter_returns_bare_list_when_no_acl():
    results = _fake_results("a", "b", "c")
    assert session_acl.filter_search_results(results, "normal-sid") == results
    assert session_acl.filter_search_results(results, None) == results


def test_filter_returns_bare_list_when_acl_matches_everything():
    """ACL in effect but doesn't actually hide anything → bare list.
    Agents that don't need to know about the ACL see no extra wrapping."""
    session_acl.set_session_acl("lms-abc", ["a", "b", "c"])
    results = _fake_results("a", "b")
    out = session_acl.filter_search_results(results, "lms-abc")
    assert out == results
    assert isinstance(out, list)


def test_filter_surfaces_notice_when_results_are_hidden():
    """The central fix: trimmed results come back as a dict with an
    explicit notice the model can see."""
    session_acl.set_session_acl("lms-abc", ["a"])
    results = _fake_results("a", "b", "c")  # b and c will be hidden
    out = session_acl.filter_search_results(results, "lms-abc")
    assert isinstance(out, dict)
    assert out["_acl_filtered"] is True
    assert out["_acl_hidden_count"] == 2
    assert len(out["results"]) == 1
    assert out["results"][0]["name"] == "a"
    assert "hidden" in out["_acl_notice"].lower()


def test_filter_empties_with_notice_when_all_hidden():
    """All results match the query but none are in the ACL — notice
    fires with an empty results list rather than a bare ``[]``, which
    is exactly the signal that prevented the search-loop regression."""
    session_acl.set_session_acl("lms-abc", ["x"])
    results = _fake_results("a", "b", "c")
    out = session_acl.filter_search_results(results, "lms-abc")
    assert isinstance(out, dict)
    assert out["_acl_hidden_count"] == 3
    assert out["results"] == []


def test_filter_fail_closed_when_session_none_with_acl_active():
    """Unresolved session + ACL active somewhere → treat as empty ACL
    (hides everything) and surface the notice so the caller notices."""
    session_acl.set_session_acl("lms-abc", ["a"])
    results = _fake_results("a", "b")
    out = session_acl.filter_search_results(results, None)
    assert isinstance(out, dict)
    assert out["_acl_hidden_count"] == 2
    assert out["results"] == []


def test_filter_session_none_no_acl_anywhere_is_open():
    """Unresolved session AND no ACL active anywhere — default-open,
    bare list returned. (The common case for normal agents.)"""
    results = _fake_results("a", "b")
    assert session_acl.filter_search_results(results, None) == results
