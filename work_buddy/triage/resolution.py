"""Resolution-Surface taxonomies and derivation (Slice 1.5).

The Resolution Surface presents agent uncertainty back to the user as
type-aware cards. Each card has a ``resolution_type`` selecting the
renderer the frontend dispatches to, and (optionally) a typed
``pipeline_blocker`` explaining *why* the agent stopped.

Slice 1.5 ships the foundational types — ``verdict_review`` and
``raw_capture`` — plus declares the rest as forward-compat strings
that downstream slices (4, 6, 7) will activate. The frontend renders
unsupported types as a graceful "ships with Slice X" placeholder
rather than blowing up.

This module is the single source of truth for the strings; the
backend stamps them on the presentation, the frontend dispatches on
them. Keep both sides aligned by importing from here, not by
hard-coding the literal strings elsewhere.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Resolution-type taxonomy
# ---------------------------------------------------------------------------

# Frontend renders these today.
RESOLUTION_TYPE_VERDICT_REVIEW = "verdict_review"
RESOLUTION_TYPE_RAW_CAPTURE = "raw_capture"

# Forward-compat — declared now so the dispatcher table is closed; renderers
# return a "ships with Slice X" placeholder until their owning slice lands.
RESOLUTION_TYPE_CLARIFICATION = "clarification"      # Slice 3 wires up
RESOLUTION_TYPE_PLACEMENT = "placement"              # Slice 6 wires up
RESOLUTION_TYPE_DECOMPOSITION = "decomposition"      # Slice 7 wires up
RESOLUTION_TYPE_PLAN_APPROVAL = "plan_approval"      # Slice 4 wires up
RESOLUTION_TYPE_OUTPUT_REVIEW = "output_review"      # Slice 4 wires up

RESOLUTION_TYPES = (
    RESOLUTION_TYPE_VERDICT_REVIEW,
    RESOLUTION_TYPE_RAW_CAPTURE,
    RESOLUTION_TYPE_CLARIFICATION,
    RESOLUTION_TYPE_PLACEMENT,
    RESOLUTION_TYPE_DECOMPOSITION,
    RESOLUTION_TYPE_PLAN_APPROVAL,
    RESOLUTION_TYPE_OUTPUT_REVIEW,
)


def derive_resolution_type(verdict: dict[str, Any]) -> str:
    """Pick a resolution_type for a pool entry from its verdict shape.

    Discrimination order (most specific first):

    - ``clarification`` — verdict has a non-empty ``refusal`` dict
      (Slice 3). The agent declined to commit a verdict; the user
      needs to answer a question to unblock.
    - ``raw_capture`` — verdict has ``raw == True`` (Slice 1).
      Verdict-pass was disabled; user picks an action manually.
    - ``verdict_review`` — anything else, including Slice 3
      multi-record verdicts. The Resolution Surface card renders
      the records list (or the legacy single action) for review.

    Slices 4/6/7 will introduce ``placement`` / ``decomposition`` /
    ``plan_approval`` / ``output_review`` as their owning data lands.
    The function is intentionally minimal — adding a discriminator
    here is cheap; reading verdict shapes that don't exist yet is a
    design fork-trap.
    """
    if not isinstance(verdict, dict):
        return RESOLUTION_TYPE_VERDICT_REVIEW
    if isinstance(verdict.get("refusal"), dict) and verdict["refusal"].get("question"):
        return RESOLUTION_TYPE_CLARIFICATION
    if verdict.get("raw") is True:
        return RESOLUTION_TYPE_RAW_CAPTURE
    return RESOLUTION_TYPE_VERDICT_REVIEW


# ---------------------------------------------------------------------------
# Pipeline blockers (ROADMAP §3.3)
# ---------------------------------------------------------------------------

# Surfaced on Resolution Surface cards as typed badges. The frontend
# renders the human-readable label + (optionally) a deep-link affordance
# (e.g. setup-wizard URL) for the user to act on.

PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET = "agent_context_unmet"
PIPELINE_BLOCKER_USER_CONTEXT_UNMET = "user_context_unmet"
PIPELINE_BLOCKER_CONSENT_REQUIRED = "consent_required"
PIPELINE_BLOCKER_CLARIFICATION_REQUIRED = "clarification_required"
PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED = "risk_threshold_exceeded"
PIPELINE_BLOCKER_INFERENCE_UNCERTAIN = "inference_uncertain"

PIPELINE_BLOCKERS = (
    PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET,
    PIPELINE_BLOCKER_USER_CONTEXT_UNMET,
    PIPELINE_BLOCKER_CONSENT_REQUIRED,
    PIPELINE_BLOCKER_CLARIFICATION_REQUIRED,
    PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED,
    PIPELINE_BLOCKER_INFERENCE_UNCERTAIN,
)

# Per-blocker presentation hints the frontend uses when rendering a
# card's blocker badge. The frontend imports these as JSON via the
# /api/review payload — no JS-side hard-coded mapping. ``deep_link``
# is a relative URL the frontend turns into an action button; null
# means no actionable affordance, just the explanatory label.
PIPELINE_BLOCKER_PRESENTATION: dict[str, dict[str, Any]] = {
    PIPELINE_BLOCKER_AGENT_CONTEXT_UNMET: {
        "label": "Agent missing a tool / access",
        "tone": "blocked",
        "deep_link": "/setup",
        "deep_link_label": "Open setup wizard",
    },
    PIPELINE_BLOCKER_USER_CONTEXT_UNMET: {
        "label": "Waiting for the right context",
        "tone": "deferred",
        "deep_link": None,
    },
    PIPELINE_BLOCKER_CONSENT_REQUIRED: {
        "label": "Needs your approval",
        "tone": "blocked",
        "deep_link": None,
    },
    PIPELINE_BLOCKER_CLARIFICATION_REQUIRED: {
        "label": "Needs a clarification",
        "tone": "info",
        "deep_link": None,
    },
    PIPELINE_BLOCKER_RISK_THRESHOLD_EXCEEDED: {
        "label": "Risk above your tolerance",
        "tone": "blocked",
        "deep_link": None,
    },
    PIPELINE_BLOCKER_INFERENCE_UNCERTAIN: {
        "label": "Agent isn't confident",
        "tone": "info",
        "deep_link": None,
    },
}


def extract_pipeline_blocker(verdict: dict[str, Any]) -> dict[str, Any] | None:
    """Pull a typed pipeline-blocker descriptor off a verdict, if any.

    Returns a dict ready to embed in a presentation_group::

        {"kind": "<blocker_id>", "label": "...", "tone": "...",
         "deep_link": "/setup" | None, "deep_link_label": "..." | None,
         "detail": "<verdict-supplied free text>" | None}

    Verdicts can declare a blocker via ``verdict.pipeline_blocker``,
    either as a string (just the kind) or as a dict
    ``{"kind": ..., "detail": ...}``. Unknown kinds are silently
    dropped — better no badge than a misleading one.

    Returns ``None`` when no blocker is set or the kind is unknown.
    Slice 3+ will populate this on agent stops; Slice 1.5 just provides
    the read path so the frontend gets typed data when it's there.
    """
    if not isinstance(verdict, dict):
        return None
    blocker = verdict.get("pipeline_blocker")
    if not blocker:
        return None
    if isinstance(blocker, str):
        kind = blocker
        detail = None
    elif isinstance(blocker, dict):
        kind = blocker.get("kind")
        detail = blocker.get("detail")
    else:
        return None
    if kind not in PIPELINE_BLOCKER_PRESENTATION:
        return None
    base = PIPELINE_BLOCKER_PRESENTATION[kind]
    return {
        "kind": kind,
        "label": base["label"],
        "tone": base["tone"],
        "deep_link": base.get("deep_link"),
        "deep_link_label": base.get("deep_link_label"),
        "detail": detail,
    }
