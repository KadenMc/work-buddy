"""Shared LLM cluster-refinement step (Stage 4 of the unified pipeline).

Sonnet (FRONTIER_BALANCED) reviews the algorithmic clusters produced
by :meth:`SourcePipeline.precluster` and emits the final cluster set
plus a proposed action per cluster. Generalises Chrome's existing
intent-grouping (``clarify/recommend.py:group_intents``) to be
source-agnostic — the prompt template takes per-source guidance and
the action library declares which capabilities the LLM may pick from.

This module is currently a **passthrough stub**: it returns the input
``pre`` clusters unchanged with no proposed actions. The real Sonnet
implementation lands in Phase D of the rebuild plan (see
``streamed-frolicking-candy``-derived plan
``please-remove-the-tag-only-dapper-lighthouse``).

Keeping the stub means the rest of the framework (runner, action
library, per-source pipelines, dashboard wiring) can be built and
exercised against tests + a small live verification before adding
the LLM call's complexity.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from work_buddy.pipelines.types import CapturedItem, ClusterSpec

if TYPE_CHECKING:
    from work_buddy.pipelines.actions import ActionLibrary

logger = logging.getLogger(__name__)


def refine_clusters(
    items: list[CapturedItem],
    pre: list[ClusterSpec],
    *,
    source_name: str,
    action_library: "ActionLibrary",
) -> list[ClusterSpec]:
    """Refine the algorithmic clusters via LLM and propose per-cluster
    actions.

    Phase A stub: returns ``pre`` unchanged with ``proposed_action=None``
    on every cluster. Phase D replaces this with a Sonnet call that:

    1. Validates every input ``item.id`` lands in exactly one output
       cluster (no orphans, no duplicates).
    2. Picks each cluster's best action from
       ``action_library.per_group_actions()`` (or returns None when
       no action fits).
    3. Falls back to ``pre`` (with no actions) on any LLM error or
       schema-validation failure.

    Args:
        items: The annotated CapturedItems (with summary + tags filled).
        pre: Algorithmic clusters from ``precluster`` — labels +
            item_ids populated, proposed_action is None.
        source_name: ``pipeline.name`` — drives prompt variations.
        action_library: The pipeline's action library; the LLM is
            offered ``per_group_actions()`` as choices.

    Returns:
        A list of ``ClusterSpec``. In Phase A: same as ``pre``. In
        Phase D: LLM-refined.
    """
    # Stub mode — log once per call so we can spot it in operator logs
    # while Phase D is pending.
    logger.debug(
        "refine_clusters: passthrough stub (source=%s, items=%d, "
        "pre_clusters=%d, action_library=%d entries)",
        source_name, len(items), len(pre), len(action_library),
    )
    return list(pre)
