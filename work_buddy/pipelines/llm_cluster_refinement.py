"""Shared LLM cluster-refinement step (Stage 4 of the unified pipeline).

Sonnet (``tier="sonnet"``) reviews the algorithmic clusters produced
by :meth:`SourcePipeline.precluster` and emits the final cluster set
plus a proposed action per cluster. Generalises Chrome's pre-rebuild
intent-grouping (``clarify/recommend.py:group_intents``) to be
source-agnostic — the prompt template takes per-source guidance and
the action library declares which capabilities the LLM may pick from.

Failure modes
-------------

The runner treats this stage as best-effort. On any of:

- LLM timeout / API error
- Unparseable JSON response
- Schema validation failure (item_id missing / duplicated /
  capability_name not in library / confidence out of range)

…the function falls back to returning ``pre`` unchanged with no
proposed actions. The umbrella + group sub-threads still spawn; the
user can then organise via drag-drop and pick actions manually via
the column action chip.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from work_buddy.pipelines.types import (
    ActionProposal,
    CapturedItem,
    ClusterSpec,
)

if TYPE_CHECKING:
    from work_buddy.pipelines.actions import ActionLibrary

logger = logging.getLogger(__name__)


# JSON schema constraining the LLM's output. Used for the structured-
# output mode in the LLM runner; also defines what we validate
# against locally before trusting the response.
REFINE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "item_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "proposed_action": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "capability_name": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    # Range constraint enforced in
                                    # Python validator (Anthropic's
                                    # strict structured-output mode
                                    # rejects minimum/maximum).
                                    "confidence": {"type": "number"},
                                },
                                "required": ["capability_name"],
                            },
                        ],
                    },
                },
                "required": ["label", "item_ids", "proposed_action"],
            },
        },
    },
    "required": ["clusters"],
}


# Per-source prompt guidance — small dict so adding a new source
# doesn't require editing the prompt template.
SOURCE_GUIDANCE: dict[str, str] = {
    "chrome_triage": (
        "These are open Chrome tabs. Cluster by intent / topic — what "
        "the user was researching or doing. Common patterns: 'Closing "
        "all tabs after a research session' (Close all tabs), "
        "'Spinning up tasks for follow-up' (Create one task per tab "
        "or Create umbrella task), 'Saving for later' (Defer)."
    ),
    "journal_backlog": (
        "These are line-range segments from today's daily journal. "
        "Each segment is a logical unit the user wrote. Common "
        "patterns: action items the user logged ('Route to tasks'), "
        "project observations or research notes ('Append to a note' "
        "with the project's main note), open questions or decisions "
        "deferred ('Route to considerations'), passing thoughts not "
        "worth acting on ('Dismiss')."
    ),
}


def refine_clusters(
    items: list[CapturedItem],
    pre: list[ClusterSpec],
    *,
    source_name: str,
    action_library: "ActionLibrary",
) -> list[ClusterSpec]:
    """Refine algorithmic clusters via Sonnet + propose per-cluster
    actions.

    Returns:
        A list of :class:`ClusterSpec`. On any failure: ``pre``
        unchanged with no action proposals.
    """
    if not pre or not items:
        return list(pre)

    per_group_actions = action_library.per_group_actions()
    if not per_group_actions:
        # Library has no actions to propose — still ask the LLM to
        # clean up cluster boundaries + labels, but tell it
        # ``proposed_action`` must be null for every cluster.
        pass

    try:
        response = _call_llm(
            items=items,
            pre=pre,
            source_name=source_name,
            per_group_actions=[d.to_dict() for d in per_group_actions],
        )
    except Exception as e:
        logger.warning(
            "refine_clusters [%s]: LLM call raised %s; falling back to "
            "algorithmic clusters",
            source_name, e,
        )
        return list(pre)

    if response is None:
        return list(pre)

    parsed, tier_used, model_used = response
    try:
        validated = _validate_and_assemble(
            parsed,
            items=items,
            action_library=action_library,
            tier_used=tier_used,
            model_used=model_used,
        )
    except _ValidationError as e:
        logger.warning(
            "refine_clusters [%s]: response invalid (%s); falling back to "
            "algorithmic clusters",
            source_name, e,
        )
        return list(pre)

    logger.info(
        "refine_clusters [%s]: %d input clusters → %d output; "
        "%d carry proposed actions",
        source_name, len(pre), len(validated),
        sum(1 for c in validated if c.proposed_action is not None),
    )
    return validated


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _ValidationError(ValueError):
    """Raised internally when the LLM response fails our schema /
    item-id-cover invariants."""


def _call_llm(
    *,
    items: list[CapturedItem],
    pre: list[ClusterSpec],
    source_name: str,
    per_group_actions: list[dict[str, Any]],
) -> tuple[dict[str, Any], str | None, str | None] | None:
    """Render the prompt + run the LLM.

    Returns ``(parsed, tier_used, model_used)`` on success, or ``None`` on
    any failure. ``tier_used`` is the resolved :class:`ModelTier` value
    string (e.g. ``"frontier_balanced"``); ``model_used`` is the concrete
    model identifier the backend reported (e.g.
    ``"claude-sonnet-4-5"``). Both flow into per-cluster
    :class:`ActionProposal` instances so the synthetic ``action_inferred``
    event records its true provenance.
    """
    from work_buddy.llm import LLMRunner, ModelTier

    system = _render_system_prompt(source_name, per_group_actions)
    user = _render_user_payload(items, pre, per_group_actions)

    resp = LLMRunner().call(
        tier=ModelTier.FRONTIER_BALANCED,
        system=system,
        user=user,
        output_schema=REFINE_OUTPUT_SCHEMA,
        max_tokens=4096,
        temperature=0.2,
        cache_ttl_minutes=0,
        trace_id=f"refine_clusters_{source_name}",
    )
    if resp.is_error():
        logger.warning(
            "refine_clusters [%s]: LLMRunner returned error: %s",
            source_name, resp.error,
        )
        return None
    parsed = resp.structured_output
    if not isinstance(parsed, dict):
        logger.warning(
            "refine_clusters [%s]: LLMRunner returned no structured output "
            "(content_len=%d)",
            source_name, len(resp.content or ""),
        )
        return None
    return parsed, (resp.tier_used or None), (resp.model or None)


def _render_system_prompt(
    source_name: str, per_group_actions: list[dict[str, Any]],
) -> str:
    """Render the Jinja system prompt with source-specific guidance."""
    from jinja2 import Environment, FileSystemLoader

    from work_buddy.paths import repo_root

    template_dir = repo_root() / "prompts" / "defaults"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template("cluster_refinement_system.j2")
    return template.render(
        source_label=_human_source_label(source_name),
        source_specific_guidance=SOURCE_GUIDANCE.get(source_name, ""),
        per_group_actions=per_group_actions,
    )


def _human_source_label(source_name: str) -> str:
    """Map an internal source identifier to a user-facing label
    used in the prompt's intro sentence."""
    return {
        "chrome_triage": "Chrome triage scrape",
        "journal_backlog": "daily journal scan",
    }.get(source_name, source_name.replace("_", " "))


def _render_user_payload(
    items: list[CapturedItem],
    pre: list[ClusterSpec],
    per_group_actions: list[dict[str, Any]],
) -> str:
    """Render the user message — JSON-style for clarity. Sonnet
    handles structured payloads well even without strict JSON."""
    items_payload = [
        {
            "id": ci.id,
            "label": ci.label,
            "summary": ci.summary,
            "tags": list(ci.tags or ()),
        }
        for ci in items
    ]
    clusters_payload = [
        {"label": c.label, "item_ids": list(c.item_ids)}
        for c in pre
    ]
    actions_payload = [
        {
            "capability_name": d["capability_name"],
            "label": d["label"],
            "description": d["description"],
        }
        for d in per_group_actions
    ]
    import json
    return (
        "Items:\n"
        f"{json.dumps(items_payload, indent=2)}\n\n"
        "Algorithmic clusters (initial):\n"
        f"{json.dumps(clusters_payload, indent=2)}\n\n"
        "Available per-group actions:\n"
        f"{json.dumps(actions_payload, indent=2)}\n\n"
        "Return a single JSON object matching the schema in the system "
        "prompt: ``{\"clusters\": [...]}``. Every input item id must "
        "land in exactly one output cluster."
    )


def _validate_and_assemble(
    parsed: dict[str, Any],
    *,
    items: list[CapturedItem],
    action_library: "ActionLibrary",
    tier_used: str | None = None,
    model_used: str | None = None,
) -> list[ClusterSpec]:
    """Validate the LLM JSON + build the final ClusterSpec list.

    ``tier_used`` and ``model_used`` flow into every emitted
    :class:`ActionProposal`'s provenance fields so the synthetic
    ``action_inferred`` event records who actually produced the proposal.

    Raises :class:`_ValidationError` on any invariant failure.
    """
    raw_clusters = parsed.get("clusters")
    if not isinstance(raw_clusters, list) or not raw_clusters:
        raise _ValidationError(
            "missing or empty 'clusters' array in response",
        )

    expected_ids = {ci.id for ci in items}
    seen_ids: set[str] = set()
    out: list[ClusterSpec] = []

    for idx, c in enumerate(raw_clusters):
        if not isinstance(c, dict):
            raise _ValidationError(
                f"cluster {idx}: not an object",
            )
        label = c.get("label")
        if not isinstance(label, str) or not label.strip():
            raise _ValidationError(
                f"cluster {idx}: missing or empty 'label'",
            )
        item_ids = c.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise _ValidationError(
                f"cluster {idx} ({label!r}): missing or empty 'item_ids'",
            )
        for iid in item_ids:
            if not isinstance(iid, str):
                raise _ValidationError(
                    f"cluster {idx} ({label!r}): non-string item_id",
                )
            if iid not in expected_ids:
                raise _ValidationError(
                    f"cluster {idx} ({label!r}): item_id {iid!r} not in input",
                )
            if iid in seen_ids:
                raise _ValidationError(
                    f"cluster {idx} ({label!r}): item_id {iid!r} appears in "
                    f"multiple output clusters",
                )
            seen_ids.add(iid)

        proposal = _validate_proposal(
            c.get("proposed_action"), action_library, label,
            tier_used=tier_used, model_used=model_used,
        )
        out.append(ClusterSpec(
            label=label.strip(),
            item_ids=tuple(item_ids),
            proposed_action=proposal,
        ))

    missing = expected_ids - seen_ids
    if missing:
        raise _ValidationError(
            f"items missing from output clusters: {sorted(missing)[:5]}",
        )

    return out


def _validate_proposal(
    raw: Any, action_library: "ActionLibrary", cluster_label: str,
    *,
    tier_used: str | None = None,
    model_used: str | None = None,
) -> ActionProposal | None:
    """Validate the optional ``proposed_action`` block. Returns None
    when raw is null/absent; raises on schema mismatch."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _ValidationError(
            f"cluster {cluster_label!r}: proposed_action is not an object",
        )
    capability = raw.get("capability_name")
    if not isinstance(capability, str) or not capability.strip():
        raise _ValidationError(
            f"cluster {cluster_label!r}: proposed_action.capability_name missing",
        )
    if not action_library.has(capability):
        raise _ValidationError(
            f"cluster {cluster_label!r}: proposed_action.capability_name "
            f"{capability!r} not in action library",
        )
    confidence = raw.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        raise _ValidationError(
            f"cluster {cluster_label!r}: proposed_action.confidence not numeric",
        )
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise _ValidationError(
            f"cluster {cluster_label!r}: proposed_action.confidence "
            f"{confidence!r} out of range",
        )
    rationale = raw.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        raise _ValidationError(
            f"cluster {cluster_label!r}: proposed_action.rationale not string",
        )
    return ActionProposal(
        capability_name=capability,
        parameters=dict(raw.get("parameters") or {}),
        rationale=rationale,
        confidence=confidence,
        tier_used=tier_used,
        model_used=model_used,
    )
