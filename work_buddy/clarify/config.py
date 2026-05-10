"""Feature-local config for triage pipelines.

Knobs live in CODE with an explicit override seam — the global
``config.yaml`` / ``config.local.yaml`` files should not carry every
per-task dial these pipelines produce. Most users never touch these;
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

Live consumers (post clarify -> Threads migration):

- ``segment.*`` — read by ``clarify/adapters/journal.py`` for the
  Running Notes segmenter.
- ``refine_clusters.tier_chain`` — read by
  ``pipelines/llm_cluster_refinement.py`` for the per-source pipeline
  refinement step.
- ``deadline_extract.tier_chain`` — read by
  ``clarify/deadline_extract.py`` for the inline-capture deadline
  pre-pass.
- ``adapters.<name>.*`` — per-adapter knobs read via
  :func:`adapter_config` (e.g. journal_triage's ``max_threads``).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

TRIAGE_DEFAULTS: dict[str, Any] = {
    # Pre-LLM segmentation stage (the step that tags the running
    # notes into threads). These settings apply to the llm_call
    # used inside `work_buddy.clarify.adapters.journal`.
    "segment": {
        # Profile override for the ``local_fast`` tier when running
        # segmentation. ``None`` falls through to the runner's default
        # profile binding.
        "profile": None,
        # Segmentation re-emits the entire section plus tags; a
        # reasoning-first model can easily burn 2-3k tokens on
        # chain-of-thought. Default budget leaves headroom for both.
        "max_tokens": 8192,
        "temperature": 0.0,
        # Cache TTL (minutes). Segmentation is content-keyed via
        # llm_call's cache so identical inputs don't re-bill.
        "cache_ttl_minutes": 60,
        # Tier escalation chain for the journal-adapter segmenter.
        # On validation failure (content-layer, not LLMResponse
        # error), the adapter re-issues the call at the next tier.
        # Background segmentation is mechanical grouping — Haiku is
        # usually sufficient when the local model can't; Sonnet is
        # rarely worth the spend. Add ``"frontier_balanced"`` to
        # extend to Sonnet. Empty list = single-shot at ``local_fast``.
        "tier_chain": ["local_fast", "frontier_fast"],
    },

    # Cluster-refinement stage (the LLM call inside the unified source
    # pipeline that names clusters and proposes per-cluster actions).
    # Mirrors the segmenter's ``tier_chain`` pattern: walk tiers in
    # order; on LLMRunner error OR schema-validation failure, escalate
    # to the next tier. On full exhaustion, refine_clusters falls back
    # to the algorithmic clusters with no proposed actions.
    #
    # Why local-first: the user runs a local-LLM queue specifically so
    # background pipelines don't burn API credits. Refinement is
    # structured-output classification with a small JSON schema —
    # ``local_tool_calling`` handles it well. Frontier tiers are kept
    # in the chain as graceful escalation targets when the local model
    # fails schema validation on dense or unusual scrapes.
    "refine_clusters": {
        "tier_chain": [
            "local_tool_calling",
            "local_fast",
            "frontier_fast",
            "frontier_balanced",
        ],
        "max_tokens": 4096,
        "temperature": 0.2,
    },

    # Deadline / dependency pre-pass for inline captures. Same
    # tier-chain pattern: structured-output extraction over short
    # text, walked local-first. On full exhaustion, the inline path
    # proceeds without hints (the failure-sentinel from
    # ``deadline_extract``). Schema is small (4 fields) so the local
    # tiers handle it well; frontier_fast is kept as escalation only.
    "deadline_extract": {
        "tier_chain": [
            "local_tool_calling",
            "local_fast",
            "frontier_fast",
        ],
    },

    # Text segmenter SubCall — splits a captured selection into distinct
    # *matters*. Used by `pipelines/inline.py` to detect when a single
    # right-click captures multiple unrelated subjects (e.g. "Email Bob
    # about report. Renew car insurance Friday.") and route each as its
    # own thread. The system prompt biases strongly toward "one matter"
    # to avoid false-splits; bias-toward-cohesion mirrors the project_picker.
    #
    # Operational dials live here per the standard SubCall config-key
    # pattern (see `architecture/llm-runner/decomposed-judgment`).
    "text_segmenter": {
        "tier_chain": [
            "local_tool_calling",
            "local_fast",
            "frontier_fast",
        ],
        "max_tokens": 1024,
        "temperature": 0.0,
        # Cache disabled: same text might segment differently across
        # contexts (e.g. with different hints) and we don't want stale
        # boundaries on the cheap path.
        "cache_ttl_minutes": 0,
        # Sanity guard against runaway output. The system prompt also
        # asks the model to produce at most 6 segments (cap merges).
        "max_segments": 6,
    },

    # Project picker SubCall — emits a hedged ranked-candidate list so
    # the verdict LLM can decide project assignment with broader
    # context. The "no project" option is always required to appear in
    # the candidates list (enforced by the SubCall's post-parse
    # validator); numeric thresholds for which candidate to actually
    # apply DO NOT live in Python — the verdict's reasoning over
    # candidates + broader context decides. This is intentional:
    # hardcoding a threshold below the smartest LLM in the chain forces
    # a decision at the dumbest layer.
    #
    # ``max_candidates`` is a sanity guard against runaway output; the
    # system prompt also asks the model to stop emitting candidates
    # below ~0.10 confidence.
    "project_picker": {
        "tier_chain": [
            "local_tool_calling",
            "local_fast",
            "frontier_fast",
        ],
        "max_tokens": 1024,
        "temperature": 0.0,
        # Cache disabled: active project list changes frequently
        # enough that cached candidates would shadow newly-created
        # projects.
        "cache_ttl_minutes": 0,
        "max_candidates": 5,
    },

    # Adapter-specific knobs keyed by adapter_name. Individual
    # adapters may read nested settings from here.
    "adapters": {
        "journal_triage": {
            # Upper bound on threads fed to the agent per pass.
            "max_threads": 16,
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
    """Pick the profile name for ``stage`` (``"segment"`` today).

    Override order: explicit ``override`` → stage-specific
    ``cfg[stage]["profile"]`` → ``"local_general"`` hardcoded fallback.
    """
    if override:
        return override
    stage_cfg = cfg.get(stage, {}) or {}
    stage_profile = stage_cfg.get("profile")
    if stage_profile:
        return stage_profile
    return "local_general"


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
