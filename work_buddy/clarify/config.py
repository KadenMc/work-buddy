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
    # Slice 1 verdict-pass gate. When ``enabled=False`` (the default
    # post-Slice-1, pre-Slice-3), the producer skips the LLM agent
    # invocation entirely and writes ``verdict={"raw": True}``
    # entries via :meth:`ClarifyPool.submit_raw`. Slice 3 brings
    # GTD-shaped verdicts back; until then full silence beats noisy
    # stub verdicts.
    #
    # Override: set ``triage.verdict_pass.enabled: true`` in
    # ``config.local.yaml`` to re-enable the legacy verdict pass.
    "verdict_pass": {
        "enabled": False,
    },

    # Profile used by the per-item agent loop (llm_with_tools).
    # The agent is expected to call `triage_submit` exactly once.
    "agent_profile": "local_general",

    # Pre-LLM segmentation stage (the step that tags the running
    # notes into threads). These settings apply to the llm_call
    # used inside `work_buddy.clarify.adapters.journal`.
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
        # Tier escalation chain for the journal-adapter segmenter.
        # On validation failure (content-layer, not LLMResponse
        # error), the adapter re-issues the call at the next tier.
        # Background segmentation is mechanical grouping — Haiku
        # is usually sufficient when the local model can't; Sonnet
        # is rarely worth the spend. Add ``"frontier_balanced"``
        # to extend to Sonnet. Empty list = single-shot at
        # ``local_fast``.
        "tier_chain": ["local_fast", "frontier_fast"],
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
        },
    },

    # Slice 3 presentation-layer knobs.
    #
    # Cluster-on-read groups pending pool entries into clusters via
    # ``work_buddy.clarify.cluster.cluster_items`` and (optionally)
    # labels each cluster via the Sonnet-tier ``group_intents`` call
    # in ``work_buddy.clarify.recommend``. The result lands on the
    # presentation as a ``clusters`` field; the existing
    # ``groups_by_action`` shape is preserved for backwards compat.
    #
    # Disabled by default: enabling fires real LLM calls per
    # presentation render. Flip on once the user has validated that
    # the local-pool size + Sonnet spend are acceptable.
    "presentation": {
        "cluster": {
            "enabled": False,
            # Skip clustering for pool sizes below this threshold —
            # 2 entries don't benefit from Louvain. The ``clusters``
            # field will be omitted from the presentation.
            "min_entries": 3,
            # When True, skip the Sonnet-tier group_intents call and
            # use the auto-label produced by cluster_items
            # (cohesion-and-domain-based). Lets the user see clustering
            # in action without paying for the LLM label.
            "skip_label_llm": False,
            # Override the data_type passed to group_intents. Default
            # is inferred from the dominant source ('journal' /
            # 'chrome' / 'conversation'); this lets the user pin it
            # for testing.
            "data_type": None,
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
# Verdict-pass gating (per-source override)
# ---------------------------------------------------------------------------

# Stable mapping from human-friendly config keys to triage capability
# call sites. The capability passes its own short name to
# :func:`is_verdict_pass_enabled_for`; the config key is the user-facing
# dial. ``TriageItem.source`` values (``journal_thread`` etc.) live in a
# separate namespace because they describe the *captured thing*, not the
# *user's domain*.
#
# Adding a new triage-source capability:
#   1. Pick a friendly key (e.g. ``"web_browser"`` for Chrome triage).
#   2. Add it to this dict with a one-line gloss for documentation.
#   3. Have the capability call
#      ``is_verdict_pass_enabled_for(cfg, "<your_key>")``.
#   4. Document the dial in ``config.example.yaml`` under
#      ``triage.verdict_pass.sources.*``.
KNOWN_VERDICT_PASS_SOURCES: dict[str, str] = {
    "journal":      "journal_triage_scan over Obsidian Running Notes",
    "inline":       "inline_triage_scan over send-to-agent captures",
    "email":        "email_triage_run over Thunderbird-bridge mail",
    # web_browser: chrome_triage_scan does not yet ship a verdict pass.
    # The key is reserved here so users can pre-stage the dial; it has
    # no effect until that capability lands.
    "web_browser":  "chrome_triage_scan over Chrome tab snapshots (future)",
}


def is_verdict_pass_enabled_for(cfg: dict[str, Any], source: str) -> bool:
    """Resolve the verdict-pass gate for one triage source.

    Resolution order, lowest → highest:
      1. ``False`` (default if nothing else is set).
      2. ``triage.verdict_pass.enabled`` (global default for all sources).
      3. ``triage.verdict_pass.sources.<source>.enabled`` (per-source
         override; explicitly ``true`` or ``false`` wins over the
         global default).

    Backwards-compatible: callers who keep the old global-only schema
    (``triage.verdict_pass.enabled: true``) get exactly the previous
    behavior — every source receives the same value.

    Args:
        cfg: Loaded triage config (output of :func:`load_triage_config`).
        source: One of :data:`KNOWN_VERDICT_PASS_SOURCES` keys, or any
            string. Unknown keys fall through to the global default;
            this is intentional so a typo in a per-source override
            never silently disables a real capability.

    Returns:
        ``True`` iff the verdict pass should run for this source.
    """
    vp = cfg.get("verdict_pass") or {}
    sources = vp.get("sources") or {}
    per_source = sources.get(source) or {}
    if isinstance(per_source, dict) and "enabled" in per_source:
        return bool(per_source["enabled"])
    return bool(vp.get("enabled", False))


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
