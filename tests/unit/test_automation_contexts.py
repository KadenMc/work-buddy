"""Slice 5a tests — action-context resolution layer.

Covers ``work_buddy.automation.contexts``:

1. ``parse_context_list`` / ``serialize_context_list`` accept the
   shapes the store + Clarify produce (None, JSON string, list, tuple).
2. ``CONTEXT_REGISTRY`` sentinel discipline:
   - ``None`` → user-only; agent never satisfies.
   - ``[]``   → universally available; agent always satisfies.
   - ``[...]``→ probe-gated; agent satisfies iff all listed tools
     are available now.
3. ``resolve_who_can_act`` returns the booleans + per-side unmet
   tokens + the ``agent_handoff_eligible`` flag (the
   "agent prepared X; user takes from here" framing per
   ROADMAP §3.2).
4. Unknown tokens are treated as user-only and reported in
   ``unknown_tokens`` for forward-compat (Clarify may invent a token
   before the registry catches up).
5. The user-side filter ``user_satisfies_against`` answers
   "given the user's declared current contexts, can they act?".
6. ``context_tokens_blocked_by_tool`` powers the daily-nudge
   reverse-lookup ("which contexts depend on @email_send?").
"""

from __future__ import annotations

import pytest

from work_buddy.automation.contexts import (
    CONTEXT_REGISTRY,
    WhoCanActDecision,
    context_tokens_blocked_by_tool,
    list_known_context_tokens,
    parse_context_list,
    resolve_who_can_act,
    serialize_context_list,
    user_satisfies_against,
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def test_parse_context_list_none():
    assert parse_context_list(None) == []


def test_parse_context_list_empty_string():
    assert parse_context_list("") == []
    assert parse_context_list("   ") == []


def test_parse_context_list_json_array():
    assert parse_context_list('["@filesystem", "@vault"]') == ["@filesystem", "@vault"]


def test_parse_context_list_json_object_returns_empty():
    """Defensive — store may carry a malformed value."""
    assert parse_context_list('{"oops": true}') == []


def test_parse_context_list_invalid_json_returns_empty():
    assert parse_context_list("not-json") == []


def test_parse_context_list_python_list():
    assert parse_context_list(["@filesystem"]) == ["@filesystem"]


def test_parse_context_list_drops_non_strings():
    assert parse_context_list(["@filesystem", 7, None, "@vault"]) == [
        "@filesystem", "@vault",
    ]


def test_serialize_round_trip():
    """parse(serialize(x)) == x for stable lists."""
    raw = ["@filesystem", "@vault", "@web_public"]
    assert parse_context_list(serialize_context_list(raw)) == raw


def test_serialize_dedups_preserving_order():
    out = serialize_context_list(["@vault", "@filesystem", "@vault"])
    assert parse_context_list(out) == ["@vault", "@filesystem"]


def test_serialize_none_preserves_null():
    assert serialize_context_list(None) is None


def test_serialize_empty_encodes_as_empty_array():
    assert serialize_context_list([]) == "[]"


# ---------------------------------------------------------------------------
# Registry sentinel discipline
# ---------------------------------------------------------------------------


def test_registry_user_only_tokens_map_to_none():
    """User-only contexts can never be agent-satisfied."""
    user_only = {
        "@physical", "@in_person", "@phone_voice",
        "@user_creds", "@user_workstation", "@cluster",
    }
    for tok in user_only:
        assert CONTEXT_REGISTRY[tok] is None, tok


def test_registry_universal_tokens_map_to_empty_list():
    """Universal contexts are agent-satisfiable without a probe."""
    universal = {"@filesystem", "@web_public", "@llm", "@github"}
    for tok in universal:
        assert CONTEXT_REGISTRY[tok] == [], tok


def test_registry_probe_gated_tokens_map_to_tool_lists():
    assert CONTEXT_REGISTRY["@vault"] == ["obsidian"]
    assert CONTEXT_REGISTRY["@email_send"] == ["thunderbird"]
    assert CONTEXT_REGISTRY["@email_read"] == ["thunderbird"]
    assert CONTEXT_REGISTRY["@chrome_active"] == ["chrome_extension"]


def test_list_known_context_tokens_is_sorted_and_complete():
    tokens = list_known_context_tokens()
    assert tokens == sorted(tokens)
    assert set(tokens) == set(CONTEXT_REGISTRY)


# ---------------------------------------------------------------------------
# resolve_who_can_act — universal-token paths (no probe needed)
# ---------------------------------------------------------------------------


def test_universal_contexts_agent_and_user_satisfied():
    decision = resolve_who_can_act(
        agent_required=["@filesystem"],
        user_required=["@user_workstation"],
    )
    assert decision.agent is True
    assert decision.user is True
    assert decision.blocked is False
    assert decision.agent_handoff_eligible is False


def test_empty_lists_satisfy_trivially():
    decision = resolve_who_can_act(agent_required=[], user_required=[])
    assert decision.agent is True
    assert decision.user is True
    assert decision.blocked is False


def test_none_inputs_satisfy_trivially():
    decision = resolve_who_can_act(agent_required=None, user_required=None)
    assert decision.agent is True
    assert decision.user is True


def test_json_string_inputs_parse_and_resolve():
    decision = resolve_who_can_act(
        agent_required='["@filesystem"]',
        user_required='["@user_workstation"]',
    )
    assert decision.agent is True
    assert decision.user is True


# ---------------------------------------------------------------------------
# resolve_who_can_act — user-only tokens never satisfy the agent
# ---------------------------------------------------------------------------


def test_user_only_context_blocks_agent_but_allows_user():
    """Phone-call task: user can act, agent can't — handoff card."""
    decision = resolve_who_can_act(
        agent_required=["@phone_voice"],
        user_required=["@phone_voice"],
    )
    assert decision.agent is False
    assert decision.user is True
    assert decision.blocked is False
    assert decision.agent_handoff_eligible is True
    assert "@phone_voice" in decision.agent_unmet


def test_physical_task_handoff_eligible():
    """Pure physical errand: agent has no role, user does."""
    decision = resolve_who_can_act(
        agent_required=[],
        user_required=["@physical"],
    )
    assert decision.agent is True  # empty list → always satisfied
    assert decision.user is True
    # Agent doesn't have unmet contexts; not a handoff in the
    # "user takes from here" sense. The agent is just absent.
    assert decision.agent_handoff_eligible is False


# ---------------------------------------------------------------------------
# resolve_who_can_act — probe-gated tokens (test injection)
# ---------------------------------------------------------------------------


def _status(**tools: bool) -> dict:
    """Build a tool-status dict shaped like data/runtime/tool_status.json."""
    return {tid: {"available": ok} for tid, ok in tools.items()}


def test_probe_gated_satisfies_when_tool_available():
    decision = resolve_who_can_act(
        agent_required=["@vault"],
        user_required=[],
        tool_status=_status(obsidian=True),
    )
    assert decision.agent is True
    assert decision.user is True
    assert decision.blocked is False


def test_probe_gated_unsatisfied_when_tool_down():
    decision = resolve_who_can_act(
        agent_required=["@vault"],
        user_required=[],
        tool_status=_status(obsidian=False),
    )
    assert decision.agent is False
    # User side has no requirements → user is True.
    assert decision.user is True
    assert decision.agent_unmet == ("@vault",)
    # Handoff IS eligible — the agent prepared nothing but the user
    # might be able to take it from here.  In Slice 5a the resolver
    # treats "no user_required_contexts declared" as user-can-act.
    assert decision.agent_handoff_eligible is True


def test_email_send_unsatisfied_when_thunderbird_missing():
    decision = resolve_who_can_act(
        agent_required=["@email_send"],
        user_required=["@email_send"],
        tool_status=_status(thunderbird=False),
    )
    assert decision.agent is False
    assert decision.user is True  # user-side declaration trusted
    assert decision.agent_handoff_eligible is True


def test_combined_universal_and_probe_gated():
    """Mixed list: agent satisfies iff EVERY token satisfies."""
    decision = resolve_who_can_act(
        agent_required=["@filesystem", "@vault"],
        user_required=[],
        tool_status=_status(obsidian=True),
    )
    assert decision.agent is True
    decision_down = resolve_who_can_act(
        agent_required=["@filesystem", "@vault"],
        user_required=[],
        tool_status=_status(obsidian=False),
    )
    assert decision_down.agent is False
    assert decision_down.agent_unmet == ("@vault",)


# ---------------------------------------------------------------------------
# Unknown tokens — forward-compat
# ---------------------------------------------------------------------------


def test_unknown_token_treated_as_user_only_and_flagged():
    decision = resolve_who_can_act(
        agent_required=["@quantum_lab"],
        user_required=[],
    )
    assert decision.agent is False
    assert "@quantum_lab" in decision.unknown_tokens
    assert decision.agent_unmet == ("@quantum_lab",)


def test_unknown_token_on_user_side_does_not_block_user():
    """User side trusts declared contexts — unknown tokens still satisfy."""
    decision = resolve_who_can_act(
        agent_required=[],
        user_required=["@quantum_lab"],
    )
    assert decision.user is True
    assert "@quantum_lab" in decision.unknown_tokens


# ---------------------------------------------------------------------------
# user_satisfies_against — engage-view current-context filter
# ---------------------------------------------------------------------------


def test_user_satisfies_against_empty_required_is_trivially_true():
    ok, unmet = user_satisfies_against(None, ["@filesystem"])
    assert ok is True
    assert unmet == []


def test_user_satisfies_against_subset_match():
    ok, unmet = user_satisfies_against(
        ["@user_workstation", "@filesystem"],
        ["@user_workstation", "@filesystem", "@vault"],
    )
    assert ok is True
    assert unmet == []


def test_user_satisfies_against_missing_token():
    ok, unmet = user_satisfies_against(
        ["@phone_voice"],
        ["@user_workstation"],
    )
    assert ok is False
    assert unmet == ["@phone_voice"]


# ---------------------------------------------------------------------------
# Reverse lookup
# ---------------------------------------------------------------------------


def test_context_tokens_blocked_by_tool_thunderbird():
    """When thunderbird is down, both email contexts block."""
    out = context_tokens_blocked_by_tool("thunderbird")
    assert "@email_send" in out
    assert "@email_read" in out


def test_context_tokens_blocked_by_tool_obsidian():
    out = context_tokens_blocked_by_tool("obsidian")
    assert out == ["@vault"]


def test_context_tokens_blocked_by_unknown_tool():
    """An unmapped tool ID returns an empty list."""
    assert context_tokens_blocked_by_tool("nonexistent") == []
