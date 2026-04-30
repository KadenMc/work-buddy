"""Journal Running-Notes lifecycle: extract, segment, manifest, cluster, route, rewrite.

The backlog pipeline (``/wb-journal-backlog``) chains these in order:

1. :func:`extract_running_notes` / :func:`read_running_notes` — read the
   Running Notes section from a daily journal file.
2. :func:`strip_banners` — remove carried-over banners.
3. Line-range segmentation (in
   :mod:`work_buddy.clarify.adapters.journal._segment_with_escalation`)
   — produce thread dicts via the LLM-partition + locally-assigned-id
   path.
4. :func:`build_thread_manifest` — per-thread tag/summary generation
   via :class:`LLMRunner`.
5. :func:`linearize_threads` / :func:`generate_clustered_review` —
   cluster threads by tag similarity for user review.
6. :func:`execute_routing_plan` — apply the user's routing decisions
   (create_task / create_consideration / append_to_note / delete /
   skip / split).
7. :func:`rewrite_running_notes` — write a new Running Notes section
   to the journal file with processed lines stripped (consent-gated).

The earlier tagged-text segmentation path (``validate_segmentation``,
``extract_threads``, ``repair_segmentation``) has been removed; the
line-range path replaces it. Most downstream components (clustering,
routing, manifest helpers) are substrate-agnostic and unchanged.
"""

from work_buddy.journal_backlog.clustering import (
    generate_clustered_review,
    linearize_threads,
)
from work_buddy.journal_backlog.extract import (
    extract_running_notes,
    read_running_notes,
)
from work_buddy.journal_backlog.manifest import build_thread_manifest
from work_buddy.journal_backlog.rewrite import (
    build_rewrite_preview,
    rewrite_running_notes,
)
from work_buddy.journal_backlog.route import (
    append_to_note,
    create_consideration,
    create_task,
    execute_routing_plan,
)
from work_buddy.journal_backlog.segment import (
    LINE_RANGE_SCHEMA,
    build_threads_from_line_ranges,
    generate_review_doc,
    load_manifest,
    number_lines,
    strip_banners,
    validate_line_range_segmentation,
    validate_manifest,
)

__all__ = [
    "LINE_RANGE_SCHEMA",
    "append_to_note",
    "build_rewrite_preview",
    "build_thread_manifest",
    "build_threads_from_line_ranges",
    "create_consideration",
    "create_task",
    "execute_routing_plan",
    "extract_running_notes",
    "generate_clustered_review",
    "generate_review_doc",
    "linearize_threads",
    "load_manifest",
    "number_lines",
    "read_running_notes",
    "rewrite_running_notes",
    "strip_banners",
    "validate_line_range_segmentation",
    "validate_manifest",
]
