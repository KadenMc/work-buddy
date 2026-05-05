"""Shared data types for the source-pipeline framework.

All five stages of the unified pipeline (collect → annotate →
precluster → refine → spawn) operate on these types. They're frozen
dataclasses so a captured item can be safely passed around between
stages without anyone rewriting it in place — augmentation happens by
producing a new ``CapturedItem`` with the new fields populated.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class CapturedItem:
    """A single observation pulled from a data source — a Chrome tab, a
    journal segment, or whatever future sources surface.

    Each pipeline stage may augment a CapturedItem with additional
    fields, but the original ``id``, ``source``, ``type``, ``label``,
    ``payload`` are immutable. Use :meth:`augment` (or
    ``dataclasses.replace``) to produce a new instance with stage-
    derived fields filled in.

    Fields
    ------
    id:
        Stable, source-pipeline-assigned identifier (e.g. journal hash
        ``journal_t_926fa6``, Chrome tab ID, etc.). Must be unique
        within a single pipeline run. Targeted by
        :func:`work_buddy.threads.group.move_item` when the user drags
        an item between group sub-threads.
    source:
        Short string naming the data source (``"chrome_tab"``,
        ``"journal_segment"``, ...). Same value lands on the
        :class:`~work_buddy.threads.models.ContextItem` stored on the
        eventual group sub-thread.
    type:
        Source-specific subtype (``"tab"``, ``"todo_line"``, ...).
        Used by display + cleanup adapters.
    label:
        Short human-readable description (tab title, first line of a
        journal segment). Shown on the column item card.
    payload:
        Dict of source-specific raw fields (URL, raw text, line
        numbers, ...). Pass-through; not interpreted by the
        framework itself.
    summary:
        Optional LLM-generated summary; set by ``annotate_items``.
    tags:
        Optional LLM-generated tags; set by ``annotate_items``. Drives
        algorithmic clustering signal alongside embeddings.
    embedding:
        Optional dense vector; set by ``precluster`` (or earlier).
        Tuple so the dataclass can stay frozen + hashable.
    """

    id: str
    source: str
    type: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    tags: tuple[str, ...] = ()
    embedding: tuple[float, ...] | None = None

    def augment(self, **changes: Any) -> CapturedItem:
        """Return a copy of this item with the given fields replaced.

        Convenience wrapper around :func:`dataclasses.replace` so
        pipeline stages don't have to import it explicitly.
        """
        return replace(self, **changes)

    def to_context_item(self) -> dict[str, Any]:
        """Render to the dict shape consumed by
        :func:`work_buddy.threads.group.group_thread`.

        The dict matches :class:`~work_buddy.threads.models.ContextItem`
        constructor kwargs (``ContextItem.from_dict`` accepts this).
        """
        return {
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "label": self.label,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class ActionProposal:
    """A proposed action for a group sub-thread (or a decompose-spawned
    sub-thread) — chosen by the LLM cluster-refinement step or by the
    user via the action-chip dropdown.

    The ``capability_name`` resolves to an entry in the work-buddy
    capability registry (any registered capability with
    ``is_action=True`` is a candidate). Parameters are bound at
    approval time — the runtime fills in ``item_ids`` (or whatever the
    capability's parameter schema requires) from the cluster's
    members.

    ``tier_used`` / ``model_used`` are populated when the proposal came
    from an LLM (the cluster-refinement Sonnet call); both are None for
    user-driven overrides via the action-chip dropdown. They surface in
    the synthetic ``action_inferred`` event so audit traces can attribute
    the proposal correctly.
    """

    capability_name: str
    parameters: dict[str, Any] = field(default_factory=dict)
    rationale: str | None = None
    confidence: float = 0.0
    tier_used: str | None = None
    model_used: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_name": self.capability_name,
            "parameters": dict(self.parameters),
            "rationale": self.rationale,
            "confidence": self.confidence,
            "tier_used": self.tier_used,
            "model_used": self.model_used,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActionProposal:
        return cls(
            capability_name=d["capability_name"],
            parameters=dict(d.get("parameters") or {}),
            rationale=d.get("rationale"),
            confidence=float(d.get("confidence") or 0.0),
            tier_used=d.get("tier_used"),
            model_used=d.get("model_used"),
        )


@dataclass(frozen=True)
class ClusterSpec:
    """One group's worth of items — a cluster label, the item ids it
    contains, and (optionally) a proposed action.

    Produced first by ``precluster`` (algorithmic) without
    ``proposed_action``, then refined by ``refine_clusters`` (LLM)
    which may rewrite ``label`` + ``item_ids`` + populate
    ``proposed_action``.
    """

    label: str
    item_ids: tuple[str, ...]
    proposed_action: ActionProposal | None = None

    def with_action(self, action: ActionProposal | None) -> ClusterSpec:
        return replace(self, proposed_action=action)


@dataclass(frozen=True)
class PipelineRun:
    """Result of a single end-to-end pipeline run.

    Returned by :func:`work_buddy.pipelines.runner.run_pipeline`.
    """

    pipeline_name: str
    umbrella_id: str
    child_thread_ids: tuple[str, ...]
    item_count: int
    cluster_count: int
    action_proposals: dict[str, ActionProposal] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_name": self.pipeline_name,
            "umbrella_id": self.umbrella_id,
            "child_thread_ids": list(self.child_thread_ids),
            "item_count": self.item_count,
            "cluster_count": self.cluster_count,
            "action_proposals": {
                child_id: prop.to_dict()
                for child_id, prop in self.action_proposals.items()
            },
            "error": self.error,
        }
