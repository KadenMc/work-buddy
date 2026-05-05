"""Semantic model tiers for the unified :mod:`work_buddy.llm.runner_v2` runner.

A tier is a *role* the caller wants a model to play
(``FRONTIER_BALANCED`` for considered reasoning, ``LOCAL_FAST`` for
cheap structured-output extraction), decoupled from the concrete model
ID. Configuration binds tier → model/profile under ``llm.tiers`` in
``config.yaml``; changing the bound model is a one-line config edit
with zero caller impact.

This replaces the legacy :class:`work_buddy.llm.runner.ModelTier`
(HAIKU/SONNET/OPUS), which survives during the migration as a
back-compat enum. :func:`legacy_tier_for` bridges the two — the
runner uses it when routing to the old ``run_task`` plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class ModelTier(str, Enum):
    """Semantic tier names. Bind to concrete models via ``llm.tiers`` config."""

    LOCAL_TOOL_CALLING = "local_tool_calling"
    """LM Studio or compatible local model with MCP tool-call support."""

    LOCAL_FAST = "local_fast"
    """Local model for structured output / classification. No tool calls."""

    FRONTIER_FAST = "frontier_fast"
    """Cheap frontier model — Haiku-class. Default for classify/summarize."""

    FRONTIER_BALANCED = "frontier_balanced"
    """Balanced frontier model — Sonnet-class. Default for triage reasoning."""

    FRONTIER_BEST = "frontier_best"
    """Premium frontier model — Opus-class. Reserve for hardest escalations."""

    # ---- Tiers introduced for the Thread system ----

    AGENT_HEADLESS = "agent_headless"
    """Multi-turn agent (Claude Code subprocess) with tools.

    Resumable via Thread state — a killed worker is not a problem
    because the Thread's event log is the durable source of truth.
    Stage 1 deliverable: tier registered with a stub binding so the
    queue can route to it. The actual subprocess runner lands in
    Stage 2 alongside the sidecar inference workers.
    """

    USER = "user"
    """Human-in-the-loop. "Inference at this tier" means the FSM
    transitions the Thread to a clarification state and asks the
    user via the Resolution Surface. There is no model binding —
    the runner short-circuits to a state-transition rather than
    invoking an LLM. registered; Stage 2 wires the FSM
    short-circuit.
    """


@dataclass(frozen=True)
class TierBinding:
    """Resolved configuration for a tier.

    ``backend`` selects which backend adapter the runner dispatches to.
    ``profile`` is consulted when ``backend`` is a local profile-driven
    path (lmstudio_native or openai_compat); ``model`` is consulted for
    the Anthropic backend. Exactly one of the two is meaningful per
    tier — the runner enforces this at resolution time.
    """

    tier: ModelTier
    backend: str                     # "anthropic" | "lmstudio_native" | "openai_compat"
    profile: str | None              # local profile name, or None for Anthropic
    model: str | None                # concrete model ID, or None for profile paths
    max_tokens: int
    temperature: float
    tool_support: bool               # whether the backend/model can execute tool calls


# ---------------------------------------------------------------------------
# Defaults — applied when config lacks an `llm.tiers` block for a tier.
# These mirror the existing profile/model defaults so the refactor is
# safe to land before `config.yaml` adds the `tiers` block.
# ---------------------------------------------------------------------------

_DEFAULTS: dict[ModelTier, dict[str, Any]] = {
    ModelTier.LOCAL_TOOL_CALLING: {
        "backend": "lmstudio_native",
        "profile": "local_agent",
        "model": None,
        "max_tokens": 4096,
        "temperature": 0.0,
        "tool_support": True,
    },
    ModelTier.LOCAL_FAST: {
        "backend": "openai_compat",
        "profile": "local_general",
        "model": None,
        "max_tokens": 2048,
        "temperature": 0.0,
        "tool_support": False,
    },
    ModelTier.FRONTIER_FAST: {
        "backend": "anthropic",
        "profile": None,
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "temperature": 0.0,
        "tool_support": True,
    },
    # Stub bindings — routing for these tiers is wired separately.
    ModelTier.AGENT_HEADLESS: {
        "backend": "agent_subprocess",   # special pseudo-backend; runner_v2 dispatches to a subprocess spawner in Stage 2
        "profile": None,
        "model": None,                    # the subprocess picks its own model; tier binding only flags 'route to subprocess'
        "max_tokens": 8192,
        "temperature": 0.0,
        "tool_support": True,
    },
    ModelTier.USER: {
        "backend": "user_clarification",  # special pseudo-backend; FSM short-circuits to a clarification state
        "profile": None,
        "model": None,
        "max_tokens": 0,
        "temperature": 0.0,
        "tool_support": False,
    },
    ModelTier.FRONTIER_BALANCED: {
        "backend": "anthropic",
        "profile": None,
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "temperature": 0.0,
        "tool_support": True,
    },
    ModelTier.FRONTIER_BEST: {
        "backend": "anthropic",
        "profile": None,
        "model": "claude-opus-4-6",
        "max_tokens": 8192,
        "temperature": 0.0,
        "tool_support": True,
    },
}


def resolve_tier(tier: ModelTier) -> TierBinding:
    """Merge config overrides over the defaults for ``tier``.

    Config shape (optional — missing keys fall back to the defaults)::

        llm:
          tiers:
            frontier_balanced:
              model: claude-sonnet-4-6
              defaults: {max_tokens: 8192}
            local_tool_calling:
              profile: local_agent
              defaults: {max_tokens: 4096, temperature: 0.0}
    """
    cfg = load_config().get("llm", {}).get("tiers", {}) or {}
    entry = cfg.get(tier.value, {}) or {}
    defaults = _DEFAULTS[tier]

    # Per-tier overrides — ``defaults`` subkey for numeric options, flat
    # keys for backend/profile/model. Flat keys override defaults
    # dictionary too, so callers can pin a tier entirely via config.
    entry_defaults = entry.get("defaults", {}) or {}

    return TierBinding(
        tier=tier,
        backend=entry.get("backend", defaults["backend"]),
        profile=entry.get("profile", defaults["profile"]),
        model=entry.get("model", defaults["model"]),
        max_tokens=int(entry_defaults.get("max_tokens", defaults["max_tokens"])),
        temperature=float(entry_defaults.get("temperature", defaults["temperature"])),
        tool_support=bool(entry.get("tool_support", defaults["tool_support"])),
    )


def legacy_tier_for(tier: ModelTier) -> str | None:
    """Map a new :class:`ModelTier` onto the legacy tier string.

    Returns the string used by
    :class:`work_buddy.llm.runner.ModelTier` — ``"haiku" | "sonnet"
    | "opus"`` — or ``None`` when the new tier has no legacy counterpart
    (local tiers). The runner uses this during Phase 1 to route
    frontier calls through the existing ``run_task`` plumbing.
    """
    mapping = {
        ModelTier.FRONTIER_FAST: "haiku",
        ModelTier.FRONTIER_BALANCED: "sonnet",
        ModelTier.FRONTIER_BEST: "opus",
    }
    return mapping.get(tier)
