"""Source-pipeline framework — one canonical end-to-end flow per data source.

Each data source (Chrome triage, daily-journal backlog, future others)
implements the :class:`~work_buddy.pipelines.protocol.SourcePipeline`
protocol and registers an :class:`~work_buddy.pipelines.actions.ActionLibrary`.
The shared driver in :mod:`work_buddy.pipelines.runner` runs the same
five-stage flow regardless of source:

    collect      → list of CapturedItem (raw, source-specific)
    annotate     → augment with LLM summary + tags
    precluster   → algorithmic clustering (embedding-fused)
    refine       → Sonnet review + per-cluster action proposal
    spawn        → group umbrella thread + group sub-threads + items as
                   ContextItems (via ``threads.group.group_thread``)

Per-group actions are dispatched through the standard capability
registry (``is_action=True`` entries). The same action chip UI surfaces
on group sub-thread column headers AND decompose-parent sub-thread
mini-cards, so the dashboard UX is uniform.

Public re-exports:

- :class:`CapturedItem`, :class:`ClusterSpec`, :class:`ActionProposal`,
  :class:`PipelineRun` — the shared data types
- :class:`SourcePipeline` — the per-source protocol
- :class:`ActionDescriptor`, :class:`ActionLibrary` — the action catalog primitive
- :func:`run_pipeline` — the runner
- :func:`refine_clusters` — the shared LLM step
"""

from __future__ import annotations

from work_buddy.pipelines.actions import (
    ActionDescriptor,
    ActionLibrary,
)
from work_buddy.pipelines.capability import (
    PIPELINES,
    UnknownSourceError,
    run_source_pipeline,
)
from work_buddy.pipelines.llm_cluster_refinement import refine_clusters
from work_buddy.pipelines.protocol import SourcePipeline
from work_buddy.pipelines.runner import run_pipeline
from work_buddy.pipelines.types import (
    ActionProposal,
    CapturedItem,
    ClusterSpec,
    PipelineRun,
)

__all__ = [
    "ActionDescriptor",
    "ActionLibrary",
    "ActionProposal",
    "CapturedItem",
    "ClusterSpec",
    "PIPELINES",
    "PipelineRun",
    "SourcePipeline",
    "UnknownSourceError",
    "refine_clusters",
    "run_pipeline",
    "run_source_pipeline",
]
