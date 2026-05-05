"""The ``run_source_pipeline`` capability — single MCP/wb_run entry
point that dispatches to a registered :class:`SourcePipeline` by name.

The single MCP/wb_run entry for triggering any source-pipeline run.
Slash commands and workflows that used to wire a source-specific
function call ``run_source_pipeline(source="journal_backlog", ...)``
or ``run_source_pipeline(source="chrome_triage", ...)``.

Adding a new data source (Twitter scrape, email triage backlog,
voice-memo transcripts, …) means: implement the SourcePipeline,
register it in :data:`PIPELINES`, and the same capability handles
it.
"""

from __future__ import annotations

import logging
from typing import Any

from work_buddy.pipelines.chrome import ChromeTriagePipeline
from work_buddy.pipelines.journal import JournalBacklogPipeline
from work_buddy.pipelines.protocol import SourcePipeline
from work_buddy.pipelines.runner import run_pipeline
from work_buddy.pipelines.types import PipelineRun

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry — name → factory
# ---------------------------------------------------------------------------
#
# Factories rather than instances so each invocation gets a fresh
# pipeline (avoids any accidental cross-call state if someone adds
# instance-level caches later).

PIPELINES: dict[str, type[SourcePipeline]] = {
    "chrome_triage": ChromeTriagePipeline,
    "journal_backlog": JournalBacklogPipeline,
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownSourceError(ValueError):
    """The ``source`` argument doesn't match a registered pipeline."""


# ---------------------------------------------------------------------------
# Capability entry point
# ---------------------------------------------------------------------------


def run_source_pipeline(
    *,
    source: str,
    **collect_kwargs: Any,
) -> dict[str, Any]:
    """Run the named source pipeline end-to-end.

    Args:
        source: Registered pipeline name. One of
            :data:`PIPELINES`'s keys (``"chrome_triage"``,
            ``"journal_backlog"``).
        **collect_kwargs: Forwarded to the pipeline's
            :meth:`SourcePipeline.collect`. Source-specific
            (e.g. ``journal_date`` for journal,
            ``engagement_window`` for Chrome).

    Returns:
        A serialisable dict version of :class:`PipelineRun`. Keys::

            {
              "pipeline_name": str,
              "umbrella_id": str,
              "child_thread_ids": [str, ...],
              "item_count": int,
              "cluster_count": int,
              "action_proposals": {child_id: {capability_name, ...}},
              "error": str | None
            }

    Raises:
        UnknownSourceError: ``source`` isn't in :data:`PIPELINES`.
    """
    factory = PIPELINES.get(source)
    if factory is None:
        raise UnknownSourceError(
            f"Unknown source pipeline {source!r}. "
            f"Registered: {sorted(PIPELINES)}"
        )

    pipeline = factory()
    logger.info(
        "run_source_pipeline: dispatching to %s with kwargs=%s",
        source, list(collect_kwargs),
    )
    result: PipelineRun = run_pipeline(pipeline, **collect_kwargs)
    return result.to_dict()
