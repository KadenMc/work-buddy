"""The ``SourcePipeline`` protocol — what every data source registers.

A concrete pipeline (e.g. :class:`~work_buddy.pipelines.journal.JournalBacklogPipeline`,
:class:`~work_buddy.pipelines.chrome.ChromeTriagePipeline`) implements
the four stage methods plus declares its
:class:`~work_buddy.pipelines.actions.ActionLibrary`. The shared
:func:`work_buddy.pipelines.runner.run_pipeline` driver runs the same
flow regardless of source.

This is a Protocol (structural typing), not an ABC, so concrete
classes don't need to inherit — they just satisfy the shape. Tests
can substitute lightweight stand-ins without dependency on the real
LLM/embedding/store layers.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from work_buddy.pipelines.actions import ActionLibrary
from work_buddy.pipelines.types import CapturedItem, ClusterSpec


@runtime_checkable
class SourcePipeline(Protocol):
    """Per-source pipeline contract.

    Stage methods correspond to the five-step flow in the runner:

    - :meth:`collect` (Stage 1): pull raw items from the source.
    - :meth:`annotate_items` (Stage 2, optional LLM): augment each
      item with summary + tags.
    - :meth:`precluster` (Stage 3, algorithmic): produce candidate
      clusters via embedding-fused similarity (or whatever signal the
      source supports).
    - :meth:`umbrella_summary` (Stage 5 helper): build the
      ``inciting_event_summary`` for the umbrella thread.

    Stage 4 (LLM cluster refinement) is shared across sources and lives
    in :func:`work_buddy.pipelines.refine_clusters` — pipelines don't
    implement it themselves; they only declare the action library it
    can choose from.
    """

    name: str
    """Short string identifier — ``"chrome_triage"``, ``"journal_backlog"``,
    etc. Surfaces in event audit data + the umbrella's
    ``inciting_event_summary["source"]``."""

    @property
    def action_library(self) -> ActionLibrary:
        """The set of actions this source's group sub-threads can carry.

        Merged with universal actions (dismiss / defer / rename /
        approve-individually) by the runner.
        """
        ...

    def collect(self, **kwargs: Any) -> list[CapturedItem]:
        """Stage 1: pull raw items from the source.

        Free-form kwargs — each pipeline declares what it accepts
        (e.g. ``journal_date`` for journal, ``engagement_window`` for
        Chrome). The runner forwards everything.

        Empty list is allowed; the runner will still spawn an umbrella
        with zero children so the user sees that the run executed.
        """
        ...

    def annotate_items(
        self, items: list[CapturedItem],
    ) -> list[CapturedItem]:
        """Stage 2: augment each item with summary + tags.

        Implementations may call an LLM (Haiku tier is the default for
        this stage) or use cheaper signals (e.g. extract inline tags
        from raw text). Failures should degrade gracefully — return
        items with empty ``summary``/``tags`` rather than raising,
        unless the failure is truly fatal.

        MAY return the same list if no augmentation is desired.
        """
        ...

    def precluster(
        self, items: list[CapturedItem],
    ) -> list[ClusterSpec]:
        """Stage 3: cluster items algorithmically.

        Returns a list of :class:`ClusterSpec` with ``label`` + ``item_ids``
        populated; ``proposed_action`` is left ``None`` (the LLM
        refinement step in Stage 4 fills that in).

        Embedding-driven clustering is preferred. On embedding-service
        unavailability the pipeline should degrade to a tag-only or
        proximity-only fallback rather than raising.

        Empty input → empty output is acceptable.
        """
        ...

    def dedup_key(
        self,
        items: list[CapturedItem],
        run_metadata: dict[str, Any],
    ) -> Optional[str]:
        """Optional cross-run dedup identifier.

        Return a stable, source-namespaced string identifying the
        logical scope of this run (e.g. ``"journal_backlog:2026-05-13"``).
        The runner short-circuits the umbrella spawn when an open
        umbrella with the same key already exists, avoiding duplicate
        top-level threads when a scheduled job re-fires on the same
        scope.

        Returning ``None`` (the default behavior for pipelines that
        don't override) means "no dedup — spawn unconditionally."

        Keys must be source-prefixed so unrelated pipelines can't
        collide on a coincidental value.
        """
        return None

    def umbrella_summary(
        self, run_metadata: dict[str, Any], items: list[CapturedItem] | None = None,
    ) -> dict[str, Any]:
        """Build the umbrella thread's ``inciting_event_summary``.

        ``run_metadata`` carries the kwargs that were passed to
        :meth:`collect` plus runner-derived bookkeeping (e.g.
        ``item_count``). ``items`` is the post-annotate list — passed
        so per-pipeline summaries can compute richer titles
        (e.g. distinct sender count for email triage). Pipelines that
        don't need it should accept and ignore the kwarg.

        The returned dict is stored verbatim on the umbrella's
        inciting event.

        At minimum the dict should set ``source`` (= ``self.name``)
        and ``title``; other fields are source-specific.
        """
        ...
