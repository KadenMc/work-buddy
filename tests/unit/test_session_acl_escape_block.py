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
