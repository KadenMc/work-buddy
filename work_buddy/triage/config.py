"""Feature-local config for background triage.

Knobs live in CODE with an explicit override seam — the global
``config.yaml`` / ``config.local.yaml`` files should not carry every
per-task dial this feature produces. Most users never touch these;
the few who do get surgical overrides without polluting the global
config surface.

Override model (lowest → highest priority):

  1. :data:`TRIAGE_DEFAULTS` in this file (shipped defaults)
  2. ``triage:`` block in ``config.yaml`` (repo-shared override)
  3. ``triage:`` block in ``config.local.yaml`` (user override)

Steps 2 and 3 are merged automatically by
:func:`work_buddy.config.load_config` — this module just deep-merges
the result over the code defaults. Users needing to tune, for
example, segmentation ``max_tokens`` on a reasoning-heavy local
model add only the specific key they care about::

    # config.local.yaml
    triage:
      segment:
        max_tokens: 8192

Everything else stays at the code-defined default.

The global ``config.example.yaml`` advertises only the most common
override (``agent_profile``) so the file stays scannable. Anyone
hunting for the full knob list lands here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

TRIAGE_DEFAULTS: dict[str, Any] = {
    # Profile used by the per-item agent loop (llm_with_tools).
    # The agent is expected to call `triage_submit` exactly once.
    "agent_profile": "local_general",

    # Pre-LLM segmentation stage (the step that tags the running
    # notes into threads). These settings apply to the llm_call
    # used inside `work_buddy.triage.adapters.journal`.
    "segment": {
        # Falls through to ``agent_profile`` when unset — split
        # only if you want a longer-context / different model.
        "profile": None,
        # Segmentation re-emits the entire section plus tags; a
        # reasoning-first model can easily burn 2-3k tokens on
        # chain-of-thought. Default budget leaves headroom for
        # both.
        "max_tokens": 8192,
        "temperature": 0.0,
        # Cache TTL (minutes). Segmentation is content-keyed via
        # llm_call's cache so identical inputs don't re-bill.
        "cache_ttl_minutes": 60,
    },

    # Per-item agent stage (llm_with_tools). The agent gets the
    # triage_agent preset and must call triage_submit.
    "agent": {
        # Falls through to ``agent_profile`` when unset.
        "profile": None,
        "max_tokens": 1024,
        "temperature": 0.0,
    },

    # "User's current context" block injected into the per-item
    # agent prompt (active tasks / contracts / projects / recent
    # commits). Mirrors the Chrome cluster-level call's context
    # shape. Narrower-than-Chrome defaults — per-item prompts go
    # to a smaller local model that degrades on long context.
    "triage_context": {
        # Task states to include. Earlier states win the max_tasks
        # cap, so list active-work states first: focused + mit
        # show up before inbox (backlog). For users who rarely
        # promote tasks out of inbox, inbox still contributes so
        # the agent has SOMETHING to match against.
        "task_states": ["focused", "mit", "inbox"],
        # Cap total tasks after state filter. Earlier states win.
        # 12 is small enough to keep the prompt under ~1.5k tokens
        # for a typical user even with longer task titles.
        "max_tasks": 12,
        # Recent commits are usually noise for journal threads.
        "include_recent_commits": False,
    },

    # IR context enrichment per candidate.
    "enrich": {
        "enabled": True,
        "top_k": 5,
        # Truncate the query text fed to IR. A runaway thread
        # shouldn't blow up embedding cost.
        "max_text_chars": 600,
        # Optional source filter (e.g. "conversation"); None = all.
        "source": None,
    },

    # Adapter-specific knobs keyed by adapter_name. Individual
    # adapters may read nested settings from here.
    "adapters": {
        "journal_triage": {
            # Upper bound on threads fed to the agent per pass.
            "max_threads": 16,
            # ID pool handed to the segmenter. Listing 64 ids in the
            # prompt bloats input tokens and multiplies the model's
            # reasoning load for no real benefit — a single day's
            # notes rarely exceed ~8 threads. Keep this tight.
            "id_pool_size": 16,
        },
    },
}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_triage_config() -> dict[str, Any]:
    """Return the merged triage config: defaults + global ``triage:`` overrides.

    Reads ``config.yaml`` + ``config.local.yaml`` via the normal
    :func:`work_buddy.config.load_config` merge, then layers the
    ``triage`` block on top of :data:`TRIAGE_DEFAULTS`.

    Nested dicts are deep-merged; lists and scalars are replaced
    wholesale. Missing keys in user overrides silently fall back to
    the default.
    """
    try:
        from work_buddy.config import load_config
        overrides = (load_config() or {}).get("triage", {}) or {}
    except Exception:
        overrides = {}

    merged = deepcopy(TRIAGE_DEFAULTS)
    _deep_merge(merged, overrides)
    return merged


def resolve_profile(
    cfg: dict[str, Any],
    stage: str,
    override: str | None = None,
) -> str:
    """Pick the profile name for ``stage`` (``"segment"`` or ``"agent"``).

    Override order: explicit ``override`` → stage-specific
    ``cfg[stage]["profile"]`` → top-level ``cfg["agent_profile"]``.
    """
    if override:
        return override
    stage_cfg = cfg.get(stage, {}) or {}
    stage_profile = stage_cfg.get("profile")
    if stage_profile:
        return stage_profile
    return cfg.get("agent_profile", "local_general")


def adapter_config(cfg: dict[str, Any], adapter_name: str) -> dict[str, Any]:
    """Shorthand for ``cfg["adapters"][adapter_name]`` with a safe default."""
    adapters = cfg.get("adapters", {}) or {}
    return adapters.get(adapter_name, {}) or {}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """In-place deep merge: nested dicts merged, everything else overwritten."""
    for key, value in src.items():
        if (
            key in dst
            and isinstance(dst[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value
