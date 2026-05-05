"""Slice 4 resolvers: operating-tier and resurfacing-level.

This module is the single home for the pure-function gating that
decides how far an agent may take a task and how loudly the system
should resurface it.  Both functions are pure (no I/O) so they can
be unit-tested with frozen dicts and so the dashboard / engage view
can call them on every read without paying a roundtrip.

Conceptual model:

* A task carries a ``risk_profile_json`` blob with **four dimensions**
  (``financial``, ``privacy``, ``accuracy``, ``compute``) and **three
  amplifiers** (``reversibility``, ``regret_potential``,
  ``inference_uncertainty``).  The Clarify agent populates this at
  capture time; legacy / pre-Slice-4 tasks have ``None`` and fall back
  to the conservative ``SAFE_PROFILE``.
* The user carries a ``risk_tolerance`` config (per dimension) and an
  ``amplifier_policy`` (which amplifier-firings force consent).  Both
  ship in ``config.local.yaml`` under ``risk_tolerance:`` /
  ``amplifier_policy:``.
* The **operating tier** is computed at engage time as
  ``min(achievable, allowed_under_risk)``.  When an amplifier firing
  forces consent (``irreversible_requires_consent``,
  ``high_regret_requires_consent``,
  ``high_inference_uncertainty_requires_consent``), the resolver
  caps the tier at 2 ("plan-and-execute") regardless of how
  permissive the dimension tolerances are — and emits a typed
  ``pipeline_blocker`` per ROADMAP §3.3 so the surface knows *why*
  it stopped.
* The **resurfacing level** (``search_only`` / ``digest`` / ``triage``
  / ``alert``) is also computed lazily from provenance, attraction
  passes (Slice 8), relevance status (Slice 8), and deadline
  awareness — never stored.

Risk dimensions are *typed* (``categorical | currency`` ladders), so
"is X allowed?" is a one-liner against the user's tolerance.  The
``compute_*`` functions return rich decision dataclasses so callers
(dashboard, engage view, audit log) can render *why* an action was
gated, not just the final tier.

Slice 7 will introduce per-action-item risk profiles; the same
schema applies — call ``resolve_operating_tier`` with the action
item's profile instead of the parent task's.  This file stays the
single source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from work_buddy.clarify.resolution import (
    PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET,
    PIPELINE_BLOCKER_CONSENT_REQUIRED,
    PIPELINE_BLOCKER_INFERENCE_UNCERTAIN,
    PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED,
    PIPELINE_BLOCKER_USER_CONTEXT_UNMET,
)


# ---------------------------------------------------------------------------
# Risk-dimension ladders (categorical) + ordering
# ---------------------------------------------------------------------------
#
# The four dimensions split into one *currency* dimension (financial,
# measured in cents) and three *categorical* dimensions (privacy,
# accuracy, compute).  Each categorical dimension has a strictly
# ordered ladder where higher = riskier.  Tolerances and risk-profile
# values both live on these ladders, so "is risk ≤ tolerance?" is a
# numeric index comparison.

PRIVACY_LADDER: tuple[str, ...] = ("none", "internal", "public")
ACCURACY_LADDER: tuple[str, ...] = ("low_stakes", "consequential", "critical")
COMPUTE_LADDER: tuple[str, ...] = ("instant", "background", "expensive")

# Amplifier ladders.  Each amplifier has 3 levels.  ``inference_uncertainty``
# defaults to ``medium`` per Slice 4's v0 calibration plan
# (ROADMAP §7 Q-i: self-report is unreliable; medium is the safe default).
REVERSIBILITY_LADDER: tuple[str, ...] = ("trivial", "moderate", "irreversible")
REGRET_LADDER: tuple[str, ...] = ("low", "medium", "high")
INFERENCE_UNCERTAINTY_LADDER: tuple[str, ...] = ("low", "medium", "high")


# ---------------------------------------------------------------------------
# Schema dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskProfile:
    """Properties of a task action that compose against user tolerance.

    The four dimensions express *what kind of damage could happen* if
    the action goes wrong; the three amplifiers express *how much that
    damage might be magnified* by reversibility, regret, and the
    agent's calibration on user intent.
    """

    financial_cents: int = 0
    privacy: str = "none"
    accuracy: str = "low_stakes"
    compute: str = "instant"

    reversibility: str = "trivial"
    regret_potential: str = "low"
    # Default 'medium' per ROADMAP §7 Q-i v0 calibration: agent
    # self-report is unreliable, so we don't claim "low" for
    # auto-classified profiles.  Slice 3's refusal path is what
    # produces 'high'.
    inference_uncertainty: str = "medium"

    def to_json(self) -> str:
        """Serialize for storage in ``task_metadata.risk_profile_json``."""
        return json.dumps(
            {
                "financial_cents": self.financial_cents,
                "privacy": self.privacy,
                "accuracy": self.accuracy,
                "compute": self.compute,
                "reversibility": self.reversibility,
                "regret_potential": self.regret_potential,
                "inference_uncertainty": self.inference_uncertainty,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class RiskTolerance:
    """User's per-dimension ceiling for autonomous action.

    The agent may operate autonomously *up to and including* these
    levels.  Anything above forces consent-gating (which caps the
    operating tier at 2).
    """

    autonomous_max_cents: int = 0
    autonomous_privacy: str = "none"
    autonomous_accuracy: str = "low_stakes"
    autonomous_compute: str = "background"


@dataclass(frozen=True)
class AmplifierPolicy:
    """Which amplifier firings force consent regardless of dimensions."""

    irreversible_requires_consent: bool = True
    high_regret_requires_consent: bool = True
    high_inference_uncertainty_requires_consent: bool = True


# Conservative defaults, matching ROADMAP §3.4's recommended config.
SAFE_PROFILE = RiskProfile()
DEFAULT_RISK_TOLERANCE = RiskTolerance(
    autonomous_max_cents=50,
    autonomous_privacy="none",
    autonomous_accuracy="low_stakes",
    autonomous_compute="background",
)
DEFAULT_AMPLIFIER_POLICY = AmplifierPolicy(
    irreversible_requires_consent=True,
    high_regret_requires_consent=True,
    high_inference_uncertainty_requires_consent=True,
)


@dataclass(frozen=True)
class OperatingTierDecision:
    """The result of ``resolve_operating_tier``.

    Carries enough context for the dashboard, the engage view, and
    the audit log to render *why* an action was gated — not just
    whether it was.  ``allowed_under_risk`` is the per-dimension /
    per-amplifier ceiling; ``operating`` is the final ``min`` against
    achievable; ``capped_by`` lists the human-readable reasons we
    capped (each entry maps to a typed pipeline-blocker).
    """

    achievable: int
    allowed_under_risk: int
    operating: int
    pipeline_blocker: str | None = None
    capped_by: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ResurfacingDecision:
    """The result of ``compute_resurfacing_level``.

    ``level`` is one of ``search_only | digest | triage | alert``.
    ``reasons`` is a tuple of human-readable justifications used by
    the daily-review nudge ("Sparse task with deadline in 2 days —
    develop now?").
    """

    level: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


RESURFACING_LEVELS: tuple[str, ...] = (
    "search_only",
    "digest",
    "triage",
    "alert",
)


# ---------------------------------------------------------------------------
# Parsers / loaders
# ---------------------------------------------------------------------------


def parse_risk_profile(raw: str | dict[str, Any] | None) -> RiskProfile:
    """Coerce a stored ``risk_profile_json`` value into a ``RiskProfile``.

    Accepts None (legacy / not-yet-classified — returns ``SAFE_PROFILE``),
    a JSON string (the on-disk form), or an already-deserialized dict
    (the in-memory form Clarify uses).  Unknown keys are ignored;
    unknown ladder values are clamped to the safe default — better a
    quiet downgrade than a load-time crash.
    """
    if raw is None:
        return SAFE_PROFILE
    if isinstance(raw, str):
        raw_str = raw.strip()
        if not raw_str:
            return SAFE_PROFILE
        try:
            data = json.loads(raw_str)
        except json.JSONDecodeError:
            return SAFE_PROFILE
    elif isinstance(raw, Mapping):
        data = dict(raw)
    else:
        return SAFE_PROFILE

    return RiskProfile(
        financial_cents=_coerce_int(data.get("financial_cents"), default=0),
        privacy=_clamp(data.get("privacy"), PRIVACY_LADDER, default="none"),
        accuracy=_clamp(data.get("accuracy"), ACCURACY_LADDER, default="low_stakes"),
        compute=_clamp(data.get("compute"), COMPUTE_LADDER, default="instant"),
        reversibility=_clamp(
            data.get("reversibility"), REVERSIBILITY_LADDER, default="trivial",
        ),
        regret_potential=_clamp(
            data.get("regret_potential"), REGRET_LADDER, default="low",
        ),
        inference_uncertainty=_clamp(
            data.get("inference_uncertainty"),
            INFERENCE_UNCERTAINTY_LADDER,
            default="medium",
        ),
    )


def load_risk_tolerance(config: Mapping[str, Any] | None) -> RiskTolerance:
    """Read ``risk_tolerance:`` from a loaded config dict.

    Missing sections fall back to ``DEFAULT_RISK_TOLERANCE``.  This
    mirrors the existing ``work_buddy.config.load_config()`` shape
    (a nested dict).  Centralized here so the dashboard, the
    engage view, and tests all see the same defaults.
    """
    if not isinstance(config, Mapping):
        return DEFAULT_RISK_TOLERANCE
    section = config.get("risk_tolerance")
    if not isinstance(section, Mapping):
        return DEFAULT_RISK_TOLERANCE

    fin = section.get("financial") if isinstance(section.get("financial"), Mapping) else {}
    priv = section.get("privacy") if isinstance(section.get("privacy"), Mapping) else {}
    acc = section.get("accuracy") if isinstance(section.get("accuracy"), Mapping) else {}
    cmp_ = section.get("compute") if isinstance(section.get("compute"), Mapping) else {}

    return RiskTolerance(
        autonomous_max_cents=_coerce_int(
            fin.get("autonomous_max_cents"),
            default=DEFAULT_RISK_TOLERANCE.autonomous_max_cents,
        ),
        autonomous_privacy=_clamp(
            priv.get("autonomous"),
            PRIVACY_LADDER,
            default=DEFAULT_RISK_TOLERANCE.autonomous_privacy,
        ),
        autonomous_accuracy=_clamp(
            acc.get("autonomous"),
            ACCURACY_LADDER,
            default=DEFAULT_RISK_TOLERANCE.autonomous_accuracy,
        ),
        autonomous_compute=_clamp(
            cmp_.get("autonomous"),
            COMPUTE_LADDER,
            default=DEFAULT_RISK_TOLERANCE.autonomous_compute,
        ),
    )


def load_amplifier_policy(config: Mapping[str, Any] | None) -> AmplifierPolicy:
    """Read ``amplifier_policy:`` from a loaded config dict.

    Missing sections fall back to ``DEFAULT_AMPLIFIER_POLICY`` (all
    three gates ON).  Each key may be set to false in user config to
    permit autonomous action despite a high-amplifier firing — but
    the safe default is to gate.
    """
    if not isinstance(config, Mapping):
        return DEFAULT_AMPLIFIER_POLICY
    section = config.get("amplifier_policy")
    if not isinstance(section, Mapping):
        return DEFAULT_AMPLIFIER_POLICY

    return AmplifierPolicy(
        irreversible_requires_consent=_coerce_bool(
            section.get("irreversible_requires_consent"),
            default=DEFAULT_AMPLIFIER_POLICY.irreversible_requires_consent,
        ),
        high_regret_requires_consent=_coerce_bool(
            section.get("high_regret_requires_consent"),
            default=DEFAULT_AMPLIFIER_POLICY.high_regret_requires_consent,
        ),
        high_inference_uncertainty_requires_consent=_coerce_bool(
            section.get("high_inference_uncertainty_requires_consent"),
            default=DEFAULT_AMPLIFIER_POLICY.high_inference_uncertainty_requires_consent,
        ),
    )


# ---------------------------------------------------------------------------
# Achievable-tier inference
# ---------------------------------------------------------------------------


def resolve_achievable_tier(
    task: Mapping[str, Any] | None = None,
    *,
    contexts: Mapping[str, bool] | None = None,
    tool_status: Mapping[str, Any] | None = None,
) -> int:
    """Best-guess capability ceiling for a task.

    The achievable tier is the highest tier the agent's *capability*
    can support — set by inspection of the task body and which tools
    it would need.  Reads the cached ``automation_tier_achievable``
    if Clarify has populated it; otherwise infers from a combination
    of (Slice 5a) action contexts and (Slice 4) the risk profile.

    Slice 5a wiring (precedence, highest cap first):

    1. Cached value from Clarify.
    2. Physical / in-person work → tier 0.
    3. Agent can't satisfy its required contexts → tier 1
       (the agent can suggest but can't autonomously execute; the
       Resolution Surface should show a handoff card per ROADMAP §3.2
       when the user *can* satisfy their side).
    4. Risk-profile heuristic (Slice 4 v0).

    ``contexts`` is the Slice-5a forward-compat hook (kept for callers
    that already pass it through).  ``tool_status`` is for tests that
    want to bypass the live ``_TOOL_STATUS`` cache.
    """
    if not isinstance(task, Mapping):
        task = {}

    # 1. Honor an already-cached achievable tier from Clarify.
    cached = task.get("automation_tier_achievable")
    if isinstance(cached, int) and 0 <= cached <= 4:
        return cached

    # 2. Physical-world / in-person work: tier 0.  Read both fields —
    #    Slice 4 stored under ``required_contexts`` (legacy/forward-compat
    #    placeholder); Slice 5a writes ``agent_required_contexts``.
    physical_tokens = {"@physical", "@in_person", "@phone_voice"}
    for field_name in ("agent_required_contexts", "required_contexts"):
        raw = task.get(field_name)
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if isinstance(raw, list) and any(t in physical_tokens for t in raw):
            return 0

    # 3. Slice 5a context-aware cap.  When the agent can't satisfy its
    #    required contexts, the achievable ceiling is tier-1 (suggest-
    #    only) regardless of how permissive the risk profile is.  This
    #    is the live tool-state feedback — the user sets up email →
    #    dependent tasks unblock automatically on next render.
    agent_required = task.get("agent_required_contexts")
    user_required = task.get("user_required_contexts")
    if agent_required or user_required:
        try:
            from work_buddy.automation.contexts import resolve_who_can_act
            who = resolve_who_can_act(
                agent_required, user_required, tool_status=tool_status,
            )
            if not who.agent:
                # Suggest-only.  Slice 1.5's Resolution Surface picks
                # this up via the ``user_context_unmet`` /
                # ``agent_context_unmet`` blockers (see
                # resolve_operating_tier below).
                return 1
        except Exception:  # pragma: no cover — defensive
            # If the contexts module breaks, fall through to the Slice-4
            # heuristic rather than blocking task surfacing.
            pass

    # 4. Heuristic from the risk profile.  irreversible + high-regret
    #    work caps achievable at 2; critical-accuracy work caps at 3
    #    (output review is the right surface).  Everyone else can
    #    reach 3.  Tier 4 is opt-in via Clarify or by the user, never
    #    inferred.
    profile = parse_risk_profile(task.get("risk_profile_json"))
    if profile.reversibility == "irreversible" and profile.regret_potential == "high":
        return 2
    if profile.accuracy == "critical":
        return 3
    if profile.regret_potential == "high":
        return 2
    return 3


# ---------------------------------------------------------------------------
# Operating-tier resolver
# ---------------------------------------------------------------------------


def resolve_operating_tier(
    task: Mapping[str, Any] | None,
    contexts: Mapping[str, Any] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    tolerance: RiskTolerance | None = None,
    amplifier_policy: AmplifierPolicy | None = None,
    tool_status: Mapping[str, Any] | None = None,
) -> OperatingTierDecision:
    """Compute the operating tier for a task.

    Pure function — no I/O beyond the in-memory tool-status cache
    (``work_buddy.tools._TOOL_STATUS``, populated by ``probe_all``).
    All inputs are explicit:

    - ``task`` is the Slice-2/4/5a row dict (carries
      ``risk_profile_json`` plus optionally
      ``automation_tier_achievable`` and the two
      ``*_required_contexts`` lists).
    - ``contexts`` is the legacy Slice-4 forward-compat hook (unused
      today; kept in the signature so callers don't churn).
    - ``config`` is a loaded ``config.local.yaml`` dict from which
      tolerance + amplifier policy are read (callers can override
      either explicitly).
    - ``tool_status`` is a test-injection seam for the contexts
      resolver; production callers leave it None.

    Returns an ``OperatingTierDecision`` carrying the achievable
    ceiling, the per-risk ceiling, the operating tier, and a typed
    ``pipeline_blocker`` (matching ROADMAP §3.3) when the resolver
    capped below the achievable ceiling.

    Composition rule (ROADMAP §3.4): an action with high amplifiers
    requires consent *even if* the dimension levels alone would
    permit autonomy.  Three amplifiers all firing high → tier 2 cap.

    Slice 5a addition: if the task declares required contexts and the
    agent (or user) can't satisfy them, the resolver caps at tier 1
    and emits ``agent_context_unmet`` / ``user_context_unmet`` — the
    Resolution Surface uses these to render typed blocker badges with
    setup-wizard deep-links.
    """
    if not isinstance(task, Mapping):
        task = {}
    if tolerance is None:
        tolerance = load_risk_tolerance(config)
    if amplifier_policy is None:
        amplifier_policy = load_amplifier_policy(config)

    profile = parse_risk_profile(task.get("risk_profile_json"))
    achievable = resolve_achievable_tier(
        task, contexts=contexts, tool_status=tool_status,
    )

    # Slice 5a: resolve who-can-act and surface a typed blocker when
    # contexts aren't met.  We do this BEFORE the dimension/amplifier
    # walk because a context miss is a more concrete reason to stop —
    # the user can't approve a plan for an action whose tools aren't
    # set up yet.
    context_blocker: str | None = None
    context_capped_by: list[str] = []
    context_reasons: list[str] = []
    agent_required = task.get("agent_required_contexts")
    user_required = task.get("user_required_contexts")
    if agent_required or user_required:
        try:
            from work_buddy.automation.contexts import resolve_who_can_act
            who = resolve_who_can_act(
                agent_required, user_required, tool_status=tool_status,
            )
            if not who.agent and who.agent_unmet:
                context_blocker = PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET
                context_capped_by.append("contexts:agent")
                context_reasons.append(
                    "agent missing context(s): " + ", ".join(who.agent_unmet)
                )
            if not who.user and who.user_unmet:
                # Agent blocker takes priority — agent_context_unmet has
                # an actionable deep-link (setup wizard), user_context_unmet
                # only resurfaces.  Record both reasons either way.
                if context_blocker is None:
                    context_blocker = PIPELINE_BLOCKER_USER_CONTEXT_UNMET
                context_capped_by.append("contexts:user")
                context_reasons.append(
                    "user missing context(s): " + ", ".join(who.user_unmet)
                )
        except Exception:  # pragma: no cover — defensive
            pass

    capped_by: list[str] = []
    reasons: list[str] = []
    pipeline_blocker: str | None = None

    # Per-dimension allowance.  Each dimension answers: at this risk
    # level, may the agent act autonomously?  If yes → tier ≤ 4 stays
    # available; if no → tier capped at 2 (plan-and-execute) so the
    # user sees the plan before any side effect.
    allowed = 4

    if profile.financial_cents > tolerance.autonomous_max_cents:
        allowed = min(allowed, 2)
        capped_by.append("financial")
        reasons.append(
            f"financial_cents={profile.financial_cents} exceeds "
            f"autonomous_max_cents={tolerance.autonomous_max_cents}"
        )

    if _ladder_index(profile.privacy, PRIVACY_LADDER) > _ladder_index(
        tolerance.autonomous_privacy, PRIVACY_LADDER,
    ):
        allowed = min(allowed, 2)
        capped_by.append("privacy")
        reasons.append(
            f"privacy={profile.privacy} exceeds "
            f"autonomous_privacy={tolerance.autonomous_privacy}"
        )

    if _ladder_index(profile.accuracy, ACCURACY_LADDER) > _ladder_index(
        tolerance.autonomous_accuracy, ACCURACY_LADDER,
    ):
        # Accuracy is the special case: critical-accuracy work is what
        # tier-3 review-queue exists for.  Cap at 3 when accuracy
        # exceeds tolerance — show the output, don't silently commit.
        allowed = min(allowed, 3)
        capped_by.append("accuracy")
        reasons.append(
            f"accuracy={profile.accuracy} exceeds "
            f"autonomous_accuracy={tolerance.autonomous_accuracy}"
        )

    if _ladder_index(profile.compute, COMPUTE_LADDER) > _ladder_index(
        tolerance.autonomous_compute, COMPUTE_LADDER,
    ):
        allowed = min(allowed, 2)
        capped_by.append("compute")
        reasons.append(
            f"compute={profile.compute} exceeds "
            f"autonomous_compute={tolerance.autonomous_compute}"
        )

    # Amplifier policy.  Each gate caps the tier at 2 if the policy is
    # ON and the amplifier fires.  Distinct pipeline blockers per
    # cause — the surface uses the first-set blocker as the headline
    # reason for stopping (we prefer the strongest signal).
    if (
        amplifier_policy.irreversible_requires_consent
        and profile.reversibility == "irreversible"
    ):
        allowed = min(allowed, 2)
        capped_by.append("amplifier:reversibility")
        reasons.append("reversibility=irreversible forces consent (amplifier)")
        pipeline_blocker = pipeline_blocker or PIPELINE_BLOCKER_CONSENT_REQUIRED

    if (
        amplifier_policy.high_regret_requires_consent
        and profile.regret_potential == "high"
    ):
        allowed = min(allowed, 2)
        capped_by.append("amplifier:regret")
        reasons.append("regret_potential=high forces consent (amplifier)")
        pipeline_blocker = pipeline_blocker or PIPELINE_BLOCKER_CONSENT_REQUIRED

    if (
        amplifier_policy.high_inference_uncertainty_requires_consent
        and profile.inference_uncertainty == "high"
    ):
        # Inference uncertainty maps to a different blocker — the
        # agent is saying "I'm not sure I understand the user's intent
        # here," not "this is dangerous."  Surface that distinction.
        allowed = min(allowed, 2)
        capped_by.append("amplifier:inference")
        reasons.append(
            "inference_uncertainty=high forces consent (amplifier)"
        )
        pipeline_blocker = pipeline_blocker or PIPELINE_BLOCKER_INFERENCE_UNCERTAIN

    # If nothing capped via amplifiers but a dimension capped, surface
    # the risk-threshold blocker (the user can read the reasons[] for
    # which dimension fired).
    if pipeline_blocker is None and allowed < achievable:
        pipeline_blocker = PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED

    # Slice 5a: context blocker takes precedence over risk blockers
    # when the achievable tier was already pinned to 1 by the contexts
    # resolver (the agent literally can't act, so risk is moot).
    if context_blocker is not None and achievable <= 1:
        pipeline_blocker = context_blocker
        capped_by = list(capped_by) + context_capped_by
        reasons = list(reasons) + context_reasons

    operating = min(achievable, allowed)

    return OperatingTierDecision(
        achievable=achievable,
        allowed_under_risk=allowed,
        operating=operating,
        pipeline_blocker=pipeline_blocker if operating < achievable or context_blocker else None,
        capped_by=tuple(capped_by),
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Resurfacing-level resolver
# ---------------------------------------------------------------------------


def compute_resurfacing_level(
    task: Mapping[str, Any] | None,
    signals: Mapping[str, Any] | None = None,
    *,
    now_iso: str | None = None,
) -> ResurfacingDecision:
    """Compute the dynamic resurfacing level for a task.

    Pure function — same model as ``resolve_operating_tier``.  Reads
    Slice 2 provenance signals (``creation_provenance``,
    ``user_involvement``, ``creation_effort``), Slice 8 attraction
    signals (``attraction_passes``, ``relevance_status`` — when
    they arrive), and deadline awareness (``has_deadline`` +
    ``deadline_date``) to pick a level.

    Default precedence (lowest → highest):

    1. ``search_only`` — agent-inferred + low-involvement + sparse
       *and* no deadline within 14 days.  Don't surface; just keep
       in search.
    2. ``digest`` — anything that makes it past (1) but doesn't earn
       a stronger signal.  Daily summary line.
    3. ``triage`` — has_deadline within 14 days, OR
       attraction_passes ≥ 3, OR relevance_status='needs_check'.
    4. ``alert`` — has_deadline within 2 days (or already past),
       OR relevance_status='invalidated' (the world changed; user
       should see).

    The function is intentionally conservative: when in doubt about
    a signal's value (missing, malformed, NULL), default to the next
    *quieter* level.  V1a (attention scarcity) — ``alert`` is the
    most expensive surface; we earn it.

    ``signals`` may carry slice-8 fields that aren't yet on the task
    row (it's optional in the signature).  Callers (Slice 8 sidecar
    job) pass them in; today the dashboard read path passes None.
    """
    if not isinstance(task, Mapping):
        task = {}
    if not isinstance(signals, Mapping):
        signals = {}

    reasons: list[str] = []
    level = "digest"  # Default — most tasks land here

    # --- alert ladder (highest priority) -------------------------------
    has_deadline = bool(task.get("has_deadline") or signals.get("has_deadline"))
    deadline_date = task.get("deadline_date") or signals.get("deadline_date")
    days_to_deadline = _days_to_deadline(deadline_date, now_iso=now_iso)

    relevance = (
        task.get("relevance_status")
        or signals.get("relevance_status")
        or "fresh"
    )

    if relevance == "invalidated":
        return ResurfacingDecision(
            level="alert",
            reasons=("relevance_status=invalidated — world changed since capture",),
        )

    if has_deadline and days_to_deadline is not None and days_to_deadline <= 2:
        return ResurfacingDecision(
            level="alert",
            reasons=(
                f"deadline in {days_to_deadline} day(s) — surface immediately",
            ),
        )

    # --- triage ladder -------------------------------------------------
    triage_signals: list[str] = []
    if has_deadline and days_to_deadline is not None and days_to_deadline <= 14:
        triage_signals.append(
            f"deadline in {days_to_deadline} day(s) — develop at pickup"
        )
    attraction_passes = _coerce_int(
        task.get("attraction_passes") or signals.get("attraction_passes"),
        default=0,
    )
    if attraction_passes >= 3:
        triage_signals.append(
            f"attraction_passes={attraction_passes} — repeated avoidance"
        )
    if relevance == "needs_check":
        triage_signals.append(
            "relevance_status=needs_check — flagged by relevance scan"
        )

    if triage_signals:
        return ResurfacingDecision(level="triage", reasons=tuple(triage_signals))

    # --- search_only floor --------------------------------------------
    creation_effort = task.get("creation_effort") or "developed"
    user_involvement = task.get("user_involvement") or "high"
    provenance = task.get("creation_provenance") or "manual"
    is_agent_inferred = (
        provenance != "manual" and not provenance.startswith("user")
    )

    if (
        is_agent_inferred
        and creation_effort == "sparse"
        and user_involvement == "low"
        and not has_deadline
    ):
        return ResurfacingDecision(
            level="search_only",
            reasons=(
                "agent-inferred + sparse + low-involvement + no deadline — "
                "don't proactively surface (V1a attention scarcity)",
            ),
        )

    # Default — digest line in the daily summary.
    if has_deadline and days_to_deadline is not None:
        reasons.append(
            f"deadline in {days_to_deadline} day(s) — daily digest"
        )
    else:
        reasons.append("default digest level")
    return ResurfacingDecision(level=level, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ladder_index(value: Any, ladder: tuple[str, ...]) -> int:
    """Return the ladder index for ``value`` (clamped at 0 on miss).

    Used by the resolver to compare risk-level strings against
    tolerance levels.  Returns 0 (safest) for unknown values rather
    than raising — the parser layer is what enforces validity, and
    we'd rather a misclassified value not crash live engage flows.
    """
    try:
        return ladder.index(value)
    except (ValueError, TypeError):
        return 0


def _clamp(value: Any, ladder: tuple[str, ...], *, default: str) -> str:
    """Return ``value`` if it's a ladder member, else ``default``."""
    if isinstance(value, str) and value in ladder:
        return value
    return default


def _coerce_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        # bool is an int in Python — but a config value of `true`
        # silently coerces to 1, which we don't want for "max cents".
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    if isinstance(value, float):
        return int(value)
    return default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    return default


def _days_to_deadline(deadline_date: Any, *, now_iso: str | None) -> int | None:
    """Return whole-day distance to ``deadline_date``, or ``None``.

    ``deadline_date`` is the Slice-2 ISO date string (``YYYY-MM-DD``
    or full ISO datetime).  ``now_iso`` is injected for testability;
    when None, uses ``datetime.now(timezone.utc)``.  Negative values
    mean the deadline has passed (and triggers alert-level via the
    caller's ``<= 2`` check).
    """
    if not isinstance(deadline_date, str) or not deadline_date.strip():
        return None
    from datetime import datetime, timezone

    try:
        # Tolerate both 'YYYY-MM-DD' and full ISO datetimes.
        if len(deadline_date) == 10:
            deadline_dt = datetime.fromisoformat(deadline_date).replace(
                tzinfo=timezone.utc,
            )
        else:
            deadline_dt = datetime.fromisoformat(deadline_date)
            if deadline_dt.tzinfo is None:
                deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    if now_iso is None:
        now_dt = datetime.now(timezone.utc)
    else:
        try:
            now_dt = datetime.fromisoformat(now_iso)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            now_dt = datetime.now(timezone.utc)

    delta = deadline_dt - now_dt
    # Round up partial days into the more-urgent bucket: 1.5 days
    # remaining → "in 2 days" → triggers triage, not alert.
    return int(delta.total_seconds() // 86400)
