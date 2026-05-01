"""Slice 4 risk-resolver tests.

Covers the four pure-function contracts in ``work_buddy.automation.risk``:

1. ``parse_risk_profile`` accepts JSON strings, dicts, and None,
   clamping unknown values into the safe profile rather than raising.
2. ``resolve_operating_tier`` returns the achievable × allowed × min
   composition with typed pipeline blockers per ROADMAP §3.3.
3. ``resolve_operating_tier`` honours the amplifier policy:
   irreversible / high-regret / high-inference-uncertainty cap at
   tier 2 even when dimension levels alone would permit autonomy.
4. ``compute_resurfacing_level`` honours the deadline-aware ladder
   (search_only / digest / triage / alert) and the v0 default
   (digest) when no signals fire.

The user's primary spec lives in the task note for ``t-7a942b1a``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.automation.risk import (
    DEFAULT_AMPLIFIER_POLICY,
    DEFAULT_RISK_TOLERANCE,
    SAFE_PROFILE,
    AmplifierPolicy,
    RiskProfile,
    RiskTolerance,
    compute_resurfacing_level,
    load_amplifier_policy,
    load_risk_tolerance,
    parse_risk_profile,
    resolve_achievable_tier,
    resolve_operating_tier,
)
from work_buddy.clarify.resolution import (
    PIPELINE_BLOCKER_CONSENT_REQUIRED,
    PIPELINE_BLOCKER_INFERENCE_UNCERTAIN,
    PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED,
)


# ---------------------------------------------------------------------------
# parse_risk_profile
# ---------------------------------------------------------------------------


def test_parse_risk_profile_none_returns_safe_profile():
    """Legacy / pre-Slice-4 tasks store NULL — fall back to safe."""
    assert parse_risk_profile(None) == SAFE_PROFILE


def test_parse_risk_profile_blank_string_returns_safe_profile():
    assert parse_risk_profile("") == SAFE_PROFILE
    assert parse_risk_profile("   ") == SAFE_PROFILE


def test_parse_risk_profile_invalid_json_returns_safe_profile():
    """Don't crash on legacy malformed blobs — quiet downgrade."""
    assert parse_risk_profile("{not valid json") == SAFE_PROFILE


def test_parse_risk_profile_round_trip():
    profile = RiskProfile(
        financial_cents=1000,
        privacy="public",
        accuracy="critical",
        compute="expensive",
        reversibility="irreversible",
        regret_potential="high",
        inference_uncertainty="high",
    )
    parsed = parse_risk_profile(profile.to_json())
    assert parsed == profile


def test_parse_risk_profile_dict_input():
    """Clarify holds the profile as a dict before serialization."""
    parsed = parse_risk_profile({
        "financial_cents": 250,
        "privacy": "internal",
        "accuracy": "consequential",
        "compute": "background",
        "reversibility": "moderate",
        "regret_potential": "medium",
        "inference_uncertainty": "low",
    })
    assert parsed.financial_cents == 250
    assert parsed.privacy == "internal"
    assert parsed.accuracy == "consequential"
    assert parsed.regret_potential == "medium"


def test_parse_risk_profile_unknown_ladder_values_clamp_to_safe():
    """Better a quiet downgrade than a load-time crash."""
    parsed = parse_risk_profile({
        "privacy": "WAT",
        "accuracy": 12,
        "reversibility": None,
    })
    assert parsed.privacy == "none"
    assert parsed.accuracy == "low_stakes"
    assert parsed.reversibility == "trivial"


def test_parse_risk_profile_inference_uncertainty_default_medium():
    """ROADMAP §7 Q-i v0: agent self-report unreliable; default medium."""
    assert parse_risk_profile({}).inference_uncertainty == "medium"


# ---------------------------------------------------------------------------
# load_risk_tolerance / load_amplifier_policy
# ---------------------------------------------------------------------------


def test_load_risk_tolerance_missing_section_uses_defaults():
    assert load_risk_tolerance(None) == DEFAULT_RISK_TOLERANCE
    assert load_risk_tolerance({}) == DEFAULT_RISK_TOLERANCE
    assert load_risk_tolerance({"unrelated": 1}) == DEFAULT_RISK_TOLERANCE


def test_load_risk_tolerance_partial_overrides():
    """User can override one dimension and inherit the rest."""
    cfg = {
        "risk_tolerance": {
            "financial": {"autonomous_max_cents": 200},
            "privacy": {"autonomous": "internal"},
        },
    }
    tol = load_risk_tolerance(cfg)
    assert tol.autonomous_max_cents == 200
    assert tol.autonomous_privacy == "internal"
    # Unset → default.
    assert tol.autonomous_accuracy == DEFAULT_RISK_TOLERANCE.autonomous_accuracy
    assert tol.autonomous_compute == DEFAULT_RISK_TOLERANCE.autonomous_compute


def test_load_amplifier_policy_defaults_all_on():
    pol = load_amplifier_policy(None)
    assert pol.irreversible_requires_consent is True
    assert pol.high_regret_requires_consent is True
    assert pol.high_inference_uncertainty_requires_consent is True


def test_load_amplifier_policy_user_can_disable_gates():
    cfg = {
        "amplifier_policy": {
            "irreversible_requires_consent": False,
            "high_inference_uncertainty_requires_consent": False,
        },
    }
    pol = load_amplifier_policy(cfg)
    assert pol.irreversible_requires_consent is False
    assert pol.high_regret_requires_consent is True  # untouched
    assert pol.high_inference_uncertainty_requires_consent is False


def test_load_amplifier_policy_string_truthy_values():
    """YAML often parses 'yes' / 'no' as strings; coerce gracefully."""
    cfg = {
        "amplifier_policy": {
            "irreversible_requires_consent": "yes",
            "high_regret_requires_consent": "no",
        },
    }
    pol = load_amplifier_policy(cfg)
    assert pol.irreversible_requires_consent is True
    assert pol.high_regret_requires_consent is False


# ---------------------------------------------------------------------------
# resolve_achievable_tier
# ---------------------------------------------------------------------------


def test_resolve_achievable_tier_uses_cached_value():
    assert resolve_achievable_tier({"automation_tier_achievable": 2}) == 2
    assert resolve_achievable_tier({"automation_tier_achievable": 0}) == 0


def test_resolve_achievable_tier_physical_world_returns_zero():
    """Physical-world tasks bottom out at tier 0 (record + remind)."""
    profile = RiskProfile().to_json()
    task = {
        "risk_profile_json": profile,
        "agent_required_contexts": ["@physical"],
    }
    assert resolve_achievable_tier(task) == 0


def test_resolve_achievable_tier_critical_accuracy_caps_at_three():
    """Critical-accuracy work is what tier-3 review-queue exists for."""
    profile = RiskProfile(accuracy="critical").to_json()
    assert resolve_achievable_tier({"risk_profile_json": profile}) == 3


def test_resolve_achievable_tier_irreversible_high_regret_caps_at_two():
    profile = RiskProfile(
        reversibility="irreversible", regret_potential="high",
    ).to_json()
    assert resolve_achievable_tier({"risk_profile_json": profile}) == 2


def test_resolve_achievable_tier_default_is_three():
    """Most digital tasks can reach tier 3 (output review)."""
    assert resolve_achievable_tier({}) == 3


# ---------------------------------------------------------------------------
# resolve_operating_tier — basic composition
# ---------------------------------------------------------------------------


def test_resolve_operating_tier_safe_profile_lets_through():
    """Safe-profile fallback should not gate anything beyond achievable."""
    decision = resolve_operating_tier({}, tolerance=DEFAULT_RISK_TOLERANCE,
                                      amplifier_policy=DEFAULT_AMPLIFIER_POLICY)
    assert decision.achievable == 3
    assert decision.allowed_under_risk == 4
    assert decision.operating == 3
    assert decision.pipeline_blocker is None


def test_resolve_operating_tier_min_of_achievable_and_allowed():
    """Operating = min(achievable, allowed). Achievable=2 caps even if allowed=4."""
    task = {
        "automation_tier_achievable": 2,
        "risk_profile_json": RiskProfile().to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.operating == 2
    # No risk cap — capped by capability, not policy.
    assert decision.pipeline_blocker is None


def test_resolve_operating_tier_financial_dimension_caps_at_two():
    """Spend > tolerance → tier 2 cap + risk_threshold_exceeded blocker."""
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(
            financial_cents=10_000,  # > default 50¢
        ).to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.operating == 2
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED
    assert "financial" in decision.capped_by


def test_resolve_operating_tier_accuracy_caps_at_three():
    """Accuracy > tolerance: cap at 3 (output review), not 2 (plan-and-execute).

    Critical-accuracy work earns the tier-3 surface — show me the
    output. Tier-2 plan-approval is the wrong UX for "review the
    summary you wrote."
    """
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(
            accuracy="critical",
        ).to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.operating == 3
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED
    assert "accuracy" in decision.capped_by


# ---------------------------------------------------------------------------
# resolve_operating_tier — amplifiers
# ---------------------------------------------------------------------------


def test_amplifier_irreversible_caps_at_two_even_with_safe_dimensions():
    """Composition rule: amplifier fires → consent required, period.

    Even if all four dimensions are at their tolerance floors, an
    irreversible action requires consent.  Per ROADMAP §3.4.
    """
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(
            reversibility="irreversible",
        ).to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.operating == 2
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_CONSENT_REQUIRED
    assert "amplifier:reversibility" in decision.capped_by


def test_amplifier_high_regret_caps_at_two():
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(regret_potential="high").to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.operating == 2
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_CONSENT_REQUIRED


def test_amplifier_high_inference_uncertainty_uses_distinct_blocker():
    """Inference uncertainty is a different category from "this is risky"."""
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(
            inference_uncertainty="high",
        ).to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.operating == 2
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_INFERENCE_UNCERTAIN
    assert "amplifier:inference" in decision.capped_by


def test_amplifier_user_can_disable_gates_via_policy():
    """Power-user override: irreversible_requires_consent=false."""
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(
            reversibility="irreversible",
        ).to_json(),
    }
    permissive = AmplifierPolicy(
        irreversible_requires_consent=False,
        high_regret_requires_consent=True,
        high_inference_uncertainty_requires_consent=True,
    )
    decision = resolve_operating_tier(task, amplifier_policy=permissive)
    assert decision.operating == 4  # not gated
    assert decision.pipeline_blocker is None


def test_amplifier_priority_consent_over_inference_uncertainty():
    """When both reversibility AND inference fire, consent_required wins.

    Reversibility is the more concrete reason; inference uncertainty
    can shift with calibration.  Expose the strongest signal.
    """
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(
            reversibility="irreversible",
            inference_uncertainty="high",
        ).to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.pipeline_blocker == PIPELINE_BLOCKER_CONSENT_REQUIRED
    assert "amplifier:reversibility" in decision.capped_by
    assert "amplifier:inference" in decision.capped_by


def test_amplifier_three_high_amplifiers_all_fire():
    """Three amplifiers all firing high → reasons[] enumerates all of them."""
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(
            reversibility="irreversible",
            regret_potential="high",
            inference_uncertainty="high",
        ).to_json(),
    }
    decision = resolve_operating_tier(task)
    assert decision.operating == 2
    assert "amplifier:reversibility" in decision.capped_by
    assert "amplifier:regret" in decision.capped_by
    assert "amplifier:inference" in decision.capped_by


# ---------------------------------------------------------------------------
# resolve_operating_tier — config integration
# ---------------------------------------------------------------------------


def test_resolve_operating_tier_loads_tolerance_from_config():
    """When no explicit tolerance, read from config dict."""
    cfg = {
        "risk_tolerance": {
            "financial": {"autonomous_max_cents": 5_000},
        },
    }
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(financial_cents=1_000).to_json(),
    }
    # 1_000 cents < 5_000 cents tolerance → not gated.
    decision = resolve_operating_tier(task, config=cfg)
    assert decision.operating == 4
    assert decision.pipeline_blocker is None


def test_resolve_operating_tier_explicit_tolerance_overrides_config():
    """Explicit tolerance arg beats config — useful for tests."""
    cfg = {"risk_tolerance": {"financial": {"autonomous_max_cents": 100_000}}}
    strict = RiskTolerance(autonomous_max_cents=10)
    task = {
        "automation_tier_achievable": 4,
        "risk_profile_json": RiskProfile(financial_cents=50).to_json(),
    }
    decision = resolve_operating_tier(task, config=cfg, tolerance=strict)
    assert decision.operating == 2
    assert "financial" in decision.capped_by


# ---------------------------------------------------------------------------
# compute_resurfacing_level
# ---------------------------------------------------------------------------


def _iso_in_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_resurfacing_default_digest():
    """Most tasks land on digest unless something earns louder."""
    decision = compute_resurfacing_level({})
    assert decision.level == "digest"


def test_resurfacing_invalidated_relevance_is_alert():
    """The world changed — surface immediately."""
    decision = compute_resurfacing_level({"relevance_status": "invalidated"})
    assert decision.level == "alert"


def test_resurfacing_imminent_deadline_is_alert():
    """Deadline within 2 days — alert."""
    decision = compute_resurfacing_level({
        "has_deadline": True,
        "deadline_date": _iso_in_days(1),
    })
    assert decision.level == "alert"


def test_resurfacing_past_deadline_is_alert():
    """Past deadline counts as alert (delta is <= 2)."""
    decision = compute_resurfacing_level({
        "has_deadline": True,
        "deadline_date": _iso_in_days(-3),
    })
    assert decision.level == "alert"


def test_resurfacing_2week_deadline_is_triage():
    decision = compute_resurfacing_level({
        "has_deadline": True,
        "deadline_date": _iso_in_days(10),
    })
    assert decision.level == "triage"


def test_resurfacing_distant_deadline_falls_to_digest():
    decision = compute_resurfacing_level({
        "has_deadline": True,
        "deadline_date": _iso_in_days(60),
    })
    assert decision.level == "digest"


def test_resurfacing_high_attraction_passes_is_triage():
    """3+ deferrals signal the user is avoiding — earn a decision."""
    decision = compute_resurfacing_level({"attraction_passes": 5})
    assert decision.level == "triage"


def test_resurfacing_needs_check_relevance_is_triage():
    decision = compute_resurfacing_level({"relevance_status": "needs_check"})
    assert decision.level == "triage"


def test_resurfacing_agent_inferred_sparse_is_search_only():
    """Agent guessed; user wasn't there; no deadline → don't surface."""
    task = {
        "creation_provenance": "agent_inferred_from_chrome",
        "creation_effort": "sparse",
        "user_involvement": "low",
        "has_deadline": False,
    }
    decision = compute_resurfacing_level(task)
    assert decision.level == "search_only"


def test_resurfacing_agent_inferred_sparse_with_deadline_is_triage():
    """Deadline rescues low-quality captures — V2c reliable resurfacing."""
    task = {
        "creation_provenance": "agent_inferred_from_chrome",
        "creation_effort": "sparse",
        "user_involvement": "low",
        "has_deadline": True,
        "deadline_date": _iso_in_days(7),
    }
    decision = compute_resurfacing_level(task)
    assert decision.level == "triage"


def test_resurfacing_signals_kwarg_supplies_slice_8_data():
    """Slice 8 sidecar passes attraction in signals kwarg, not on the task row."""
    decision = compute_resurfacing_level(
        task={"creation_provenance": "manual"},
        signals={"attraction_passes": 4},
    )
    assert decision.level == "triage"


def test_resurfacing_now_iso_injection_for_determinism():
    """now_iso lets tests pin the deadline calc."""
    decision = compute_resurfacing_level(
        {"has_deadline": True, "deadline_date": "2026-05-15"},
        now_iso="2026-05-13T12:00:00+00:00",
    )
    assert decision.level == "alert"  # 2 days out


def test_resurfacing_invalid_deadline_string_falls_through():
    """Garbage deadline → don't surface aggressively."""
    decision = compute_resurfacing_level({
        "has_deadline": True,
        "deadline_date": "not-a-date",
    })
    assert decision.level == "digest"
