"""Slice 5a tests — risk resolver consults the contexts module.

When a task declares required contexts and the agent can't satisfy
them, ``resolve_achievable_tier`` caps at 1 (suggest only) and
``resolve_operating_tier`` surfaces an ``agent_context_unmet`` /
``user_context_unmet`` blocker per ROADMAP §3.3.

These tests inject ``tool_status`` to keep them deterministic; the
production resolver consults the live ``_TOOL_STATUS`` cache.
"""

from __future__ import annotations

from work_buddy.automation.risk import (
    resolve_achievable_tier,
    resolve_operating_tier,
)
from work_buddy.clarify.resolution import (
    PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET,
    PIPELINE_BLOCKER_USER_CONTEXT_UNMET,
)


def _status(**tools: bool) -> dict:
    return {tid: {"available": ok} for tid, ok in tools.items()}


# ---------------------------------------------------------------------------
# resolve_achievable_tier — context capping
# ---------------------------------------------------------------------------


def test_no_contexts_falls_back_to_slice_4_heuristic():
    """Tasks without context fields keep the Slice-4 risk-only behavior."""
    tier = resolve_achievable_tier({"risk_profile_json": None})
    assert tier == 3  # default heuristic


def test_agent_satisfies_contexts_no_cap():
    task = {
        "agent_required_contexts": '["@filesystem"]',
        "user_required_contexts": '[]',
    }
    tier = resolve_achievable_tier(task, tool_status=_status())
    # @filesystem is universal → agent satisfies → no context cap →
    # falls through to risk heuristic → tier 3.
    assert tier == 3


def test_agent_blocked_caps_to_tier_1():
    """Agent missing a probe-gated tool → suggest-only ceiling."""
    task = {
        "agent_required_contexts": '["@email_send"]',
        "user_required_contexts": '["@email_send"]',
    }
    tier = resolve_achievable_tier(task, tool_status=_status(thunderbird=False))
    assert tier == 1


def test_agent_satisfies_when_tool_available():
    task = {
        "agent_required_contexts": '["@email_send"]',
    }
    tier = resolve_achievable_tier(task, tool_status=_status(thunderbird=True))
    # Falls through to risk heuristic with safe profile → tier 3.
    assert tier == 3


def test_physical_context_takes_precedence_over_capability_check():
    """@physical → tier 0 always, not gated on agent satisfaction."""
    task = {
        "agent_required_contexts": '["@physical"]',
        "user_required_contexts": '["@physical"]',
    }
    tier = resolve_achievable_tier(task)
    assert tier == 0


def test_cached_achievable_overrides_context_check():
    """Pinned cached tier wins; resolver doesn't re-derive."""
    task = {
        "automation_tier_achievable": 4,
        "agent_required_contexts": '["@email_send"]',
    }
    tier = resolve_achievable_tier(task, tool_status=_status(thunderbird=False))
    assert tier == 4


# ---------------------------------------------------------------------------
# resolve_operating_tier — context blockers
# ---------------------------------------------------------------------------


def test_agent_context_unmet_emits_blocker():
    task = {
        "agent_required_contexts": '["@email_send"]',
        "user_required_contexts": '["@email_send"]',
    }
    decision = resolve_operating_tier(task, tool_status=_status(thunderbird=False))
    assert decision.achievable == 1
    assert decision.operating == 1
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET
    assert "contexts:agent" in decision.capped_by
    assert any("@email_send" in r for r in decision.reasons)


def test_user_context_unmet_alone_does_not_cap_below_3():
    """User-only @phone_voice with no agent block: agent can still suggest at tier 3."""
    task = {
        "agent_required_contexts": '[]',
        "user_required_contexts": '["@phone_voice"]',
    }
    decision = resolve_operating_tier(task)
    # Agent has no requirements (empty list), user side trusts declared
    # contexts → both satisfy → no cap → tier 3 by safe-profile heuristic.
    assert decision.operating == 3
    assert decision.pipeline_blocker is None


def test_handoff_path_caps_at_1_with_agent_blocker():
    """Agent missing a tool, user can act → tier 1 with agent_context_unmet."""
    task = {
        "agent_required_contexts": '["@vault"]',
        "user_required_contexts": '[]',  # user has nothing required
    }
    decision = resolve_operating_tier(task, tool_status=_status(obsidian=False))
    assert decision.operating == 1
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET


def test_no_context_fields_preserves_slice_4_blocker_logic():
    """Risk-amplifier-driven blockers still fire when no contexts declared.

    Use ``inference_uncertainty=high`` alone — that doesn't drop the
    achievable tier (no condition in resolve_achievable_tier matches),
    so allowed (capped at 2) is strictly below achievable (3) and the
    resolver emits the blocker.
    """
    task = {
        "risk_profile_json": (
            '{"inference_uncertainty": "high"}'
        ),
    }
    decision = resolve_operating_tier(task)
    assert decision.achievable == 3
    assert decision.operating == 2
    assert decision.pipeline_blocker is not None
