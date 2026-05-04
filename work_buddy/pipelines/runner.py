"""The shared pipeline runner — drives the five-stage flow for any
:class:`~work_buddy.pipelines.protocol.SourcePipeline`.

    collect      → annotate     → precluster    → refine        → spawn

All five stages run in order. The runner handles:

- Forwarding source-specific kwargs to ``collect``.
- Layering universal actions on top of the source's action library
  (so every group sub-thread carries a baseline set of dismiss /
  defer / rename / etc.).
- Spawning the umbrella thread + delegating to
  :func:`work_buddy.threads.group.group_thread` for the children +
  ContextItems.
- Recording each cluster's proposed action on its child sub-thread
  so the standard FSM dispatch path can pick it up at approval time.

Empty input is handled gracefully: zero items → zero clusters → the
umbrella spawns alone with no children. Operator-visible signal that
the pipeline ran.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

from work_buddy.pipelines.actions import ActionLibrary
from work_buddy.pipelines.llm_cluster_refinement import refine_clusters
from work_buddy.pipelines.types import (
    ActionProposal,
    CapturedItem,
    ClusterSpec,
    PipelineRun,
)
from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    ACTOR_INCITING,
    KIND_INCITING_EVENT,
    KIND_THREAD_CREATED,
    ThreadEvent,
)
from work_buddy.threads.models import Thread

if TYPE_CHECKING:
    from work_buddy.pipelines.protocol import SourcePipeline

logger = logging.getLogger(__name__)


def run_pipeline(
    pipeline: "SourcePipeline",
    *,
    universal_actions: ActionLibrary | None = None,
    **collect_kwargs: Any,
) -> PipelineRun:
    """Execute one end-to-end run of ``pipeline``.

    Args:
        pipeline: A concrete :class:`SourcePipeline` (e.g.
            :class:`JournalBacklogPipeline`,
            :class:`ChromeTriagePipeline`).
        universal_actions: Optional ActionLibrary of universal actions
            to layer onto the pipeline's source-specific library. If
            None, the runner imports the default universal library
            from :mod:`work_buddy.pipelines.universal_actions` (lazy
            so tests can run without the universal-actions module
            existing yet).
        **collect_kwargs: Forwarded verbatim to
            :meth:`SourcePipeline.collect`.

    Returns:
        :class:`PipelineRun` summarising what got spawned. On
        soft-failure (e.g. LLM down, group_thread refused) the run
        still returns a :class:`PipelineRun` with ``error`` populated
        — the runner doesn't raise unless the umbrella itself can't be
        created.
    """
    full_library = _build_full_library(pipeline, universal_actions)

    # Stage 1: collect.
    items: list[CapturedItem] = list(pipeline.collect(**collect_kwargs))
    logger.info(
        "pipeline.run [%s]: collected %d items",
        pipeline.name, len(items),
    )

    # Stage 2: annotate.
    if items:
        items = list(pipeline.annotate_items(items))
        logger.info(
            "pipeline.run [%s]: annotated %d items",
            pipeline.name, len(items),
        )

    # Stage 3: precluster (algorithmic).
    if items:
        pre_clusters: list[ClusterSpec] = list(pipeline.precluster(items))
        logger.info(
            "pipeline.run [%s]: precluster produced %d clusters",
            pipeline.name, len(pre_clusters),
        )
    else:
        pre_clusters = []

    # Stage 4: LLM refine + per-cluster action proposals.
    if pre_clusters:
        final_clusters = list(refine_clusters(
            items, pre_clusters,
            source_name=pipeline.name,
            action_library=full_library,
        ))
        logger.info(
            "pipeline.run [%s]: refine produced %d final clusters; "
            "%d carry proposed actions",
            pipeline.name, len(final_clusters),
            sum(1 for c in final_clusters if c.proposed_action is not None),
        )
    else:
        final_clusters = []

    # Stage 5: spawn umbrella + group children + items.
    run_metadata = dict(collect_kwargs)
    run_metadata.setdefault("source", pipeline.name)
    umbrella_summary = pipeline.umbrella_summary(run_metadata)

    umbrella_id = _spawn_umbrella(
        pipeline_name=pipeline.name,
        inciting_summary=umbrella_summary,
        item_count=len(items),
    )
    if umbrella_id is None:
        # Umbrella spawn is the only thing the runner treats as fatal;
        # without it there's nothing to anchor the children on.
        return PipelineRun(
            pipeline_name=pipeline.name,
            umbrella_id="",
            child_thread_ids=(),
            item_count=len(items),
            cluster_count=len(final_clusters),
            error="umbrella spawn failed",
        )

    if not final_clusters:
        # Empty run still produced an umbrella — surface it so the
        # user sees the pipeline executed.
        return PipelineRun(
            pipeline_name=pipeline.name,
            umbrella_id=umbrella_id,
            child_thread_ids=(),
            item_count=len(items),
            cluster_count=0,
        )

    child_ids, action_proposals = _spawn_children(
        umbrella_id=umbrella_id,
        items=items,
        clusters=final_clusters,
        pipeline_name=pipeline.name,
        run_metadata=run_metadata,
    )

    return PipelineRun(
        pipeline_name=pipeline.name,
        umbrella_id=umbrella_id,
        child_thread_ids=tuple(child_ids),
        item_count=len(items),
        cluster_count=len(final_clusters),
        action_proposals=action_proposals,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_full_library(
    pipeline: "SourcePipeline",
    universal: ActionLibrary | None,
) -> ActionLibrary:
    """Layer the pipeline's source-specific actions on top of the
    universal action set.

    Per-source descriptors win on capability_name collision (so a
    pipeline can re-skin a universal action for its domain if it
    really wants to).
    """
    if universal is None:
        try:
            from work_buddy.pipelines.universal_actions import (
                UNIVERSAL_ACTION_LIBRARY,
            )
            universal = UNIVERSAL_ACTION_LIBRARY
        except ImportError:
            # Phase A only — universal_actions module not yet built.
            # Tests can still exercise the runner by passing their
            # own universal library.
            universal = ActionLibrary([])
    return universal.merged_with(pipeline.action_library)


def _spawn_umbrella(
    *,
    pipeline_name: str,
    inciting_summary: dict[str, Any],
    item_count: int,
) -> str | None:
    """Insert the umbrella thread row + record inciting + thread_created
    events.

    The umbrella is created with ``parent_relationship='decompose'``
    initially; ``group_thread`` flips it to ``'group'`` once children
    are spawned. Empty runs (no children) leave it as decompose so the
    umbrella renders normally instead of as an empty group view.
    """
    try:
        from work_buddy.threads.autonomy import default_spawn_policy

        # Ensure required fields are present in the inciting summary.
        summary = dict(inciting_summary)
        summary.setdefault("source", pipeline_name)
        summary.setdefault("scan_id", uuid.uuid4().hex[:8])
        summary.setdefault("item_count", item_count)
        summary.setdefault("title", pipeline_name)
        summary.setdefault("description", summary["title"])

        umbrella = Thread(
            fsm_state=FSMState.MONITORING,
            inciting_event_summary=summary,
            autonomy_policy=default_spawn_policy(),
        )
        store.insert_thread(umbrella)

        e1 = store.append_event(ThreadEvent(
            thread_id=umbrella.thread_id,
            kind=KIND_INCITING_EVENT,
            actor=ACTOR_INCITING,
            data=summary,
        ))
        store.append_event(ThreadEvent(
            thread_id=umbrella.thread_id,
            kind=KIND_THREAD_CREATED,
            actor=ACTOR_INCITING,
            data={"source_pipeline": pipeline_name},
            parent_event_id=e1.id,
        ))
        store.update_thread_state(
            umbrella.thread_id,
            parent_event_id=store.latest_event_id(umbrella.thread_id),
        )
        return umbrella.thread_id
    except Exception as e:
        logger.warning(
            "pipeline.run [%s]: umbrella spawn failed: %s",
            pipeline_name, e,
        )
        return None


def _spawn_children(
    *,
    umbrella_id: str,
    items: list[CapturedItem],
    clusters: list[ClusterSpec],
    pipeline_name: str,
    run_metadata: dict[str, Any],
) -> tuple[list[str], dict[str, ActionProposal]]:
    """Delegate to ``group_thread`` to create the children + items,
    then walk the result to apply per-cluster action proposals.

    Returns ``(child_thread_ids, action_proposals_by_child)``.
    """
    from work_buddy.threads.group import GroupRefused, group_thread

    ctx_items = [item.to_context_item() for item in items]
    cluster_specs = [
        {"label": c.label, "item_ids": list(c.item_ids)}
        for c in clusters
    ]
    inciting_extra = {
        "source_pipeline": pipeline_name,
        **{k: v for k, v in run_metadata.items() if k not in ("source",)},
    }

    try:
        child_ids = group_thread(
            umbrella_id,
            ctx_items,
            cluster_specs,
            inciting_summary_extra=inciting_extra,
        )
    except GroupRefused as e:
        logger.warning(
            "pipeline.run [%s]: group_thread refused: %s",
            pipeline_name, e,
        )
        return ([], {})

    # Apply each cluster's proposed action to its corresponding child.
    # Order matches: clusters[i] → child_ids[i].
    proposals: dict[str, ActionProposal] = {}
    for cluster, child_id in zip(clusters, child_ids):
        if cluster.proposed_action is not None:
            try:
                _record_action_proposal(child_id, cluster.proposed_action)
                proposals[child_id] = cluster.proposed_action
            except Exception as e:
                logger.warning(
                    "pipeline.run [%s]: failed to record action proposal "
                    "on child %s: %s",
                    pipeline_name, child_id, e,
                )

    return (list(child_ids), proposals)


def _record_action_proposal(
    child_id: str, proposal: ActionProposal,
) -> None:
    """Append a synthetic ``action_inferred`` event on a group child
    so the standard card UI shows the proposal in the actions section.

    Mirrors what the journal/Chrome legacy paths used to do for
    ``decompose`` / ``group``-the-action — but parametrised by the
    cluster's chosen action capability.
    """
    from work_buddy.threads.events import KIND_ACTION_INFERRED

    child = store.get_thread(child_id)
    if child is None:
        raise ValueError(f"Child thread {child_id!r} not found")

    payload = {
        "kind": "standard",
        "name": proposal.capability_name,
        "parameters": dict(proposal.parameters),
        "rationale": proposal.rationale,
        "irreversibility": "low",  # Override later via per-action metadata
        "regret_potential": "low",
        "risk_amplifier": False,
    }
    store.append_event(ThreadEvent(
        thread_id=child_id,
        kind=KIND_ACTION_INFERRED,
        actor=ACTOR_INCITING,
        data={
            "target": "action",
            "payload": payload,
            "confidence": proposal.confidence,
            "tier_used": None,
            "model_used": None,
            "synthetic": True,
            "from_pipeline_proposal": True,
        },
        parent_event_id=child.parent_event_id,
    ))
    store.update_thread_state(
        child_id,
        parent_event_id=store.latest_event_id(child_id),
    )
