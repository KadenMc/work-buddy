"""Slice 4: automation tier resolution + dynamic resurfacing.

The automation subpackage owns the pure functions that decide how
much of a task an agent may execute autonomously and how loudly the
system should resurface it.  Both decisions are computed lazily from
stored signals (risk profile, achievable capability, provenance,
deadline awareness, attraction passes, …) — never stored as the
authoritative answer.  See ``work_buddy.automation.risk`` for the
schema and resolver contracts.
"""

from work_buddy.automation.risk import (
    DEFAULT_AMPLIFIER_POLICY,
    DEFAULT_RISK_TOLERANCE,
    SAFE_PROFILE,
    AmplifierPolicy,
    OperatingTierDecision,
    ResurfacingDecision,
    RiskProfile,
    RiskTolerance,
    compute_resurfacing_level,
    load_amplifier_policy,
    load_risk_tolerance,
    parse_risk_profile,
    resolve_achievable_tier,
    resolve_operating_tier,
)

__all__ = [
    "AmplifierPolicy",
    "DEFAULT_AMPLIFIER_POLICY",
    "DEFAULT_RISK_TOLERANCE",
    "OperatingTierDecision",
    "ResurfacingDecision",
    "RiskProfile",
    "RiskTolerance",
    "SAFE_PROFILE",
    "compute_resurfacing_level",
    "load_amplifier_policy",
    "load_risk_tolerance",
    "parse_risk_profile",
    "resolve_achievable_tier",
    "resolve_operating_tier",
]
