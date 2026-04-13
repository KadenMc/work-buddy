"""Journal backlog processing utilities.

Supports the ``/wb-journal-backlog`` workflow: extract Running Notes,
segment into threads, route items to destinations, and rewrite
the section with only open items remaining.
"""

from work_buddy.journal_backlog.extract import extract_running_notes, read_running_notes
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
from work_buddy.journal_backlog.clustering import (
    generate_clustered_review,
    linearize_threads,
)
from work_buddy.journal_backlog.segment import (
    extract_threads,
    generate_review_doc,
    generate_thread_ids,
    load_manifest,
    strip_banners,
    validate_manifest,
    validate_segmentation,
)

__all__ = [
    "append_to_note",
    "build_rewrite_preview",
    "create_consideration",
    "create_task",
    "execute_routing_plan",
    "extract_running_notes",
    "read_running_notes",
    "extract_threads",
    "generate_clustered_review",
    "generate_review_doc",
    "generate_thread_ids",
    "linearize_threads",
    "load_manifest",
    "rewrite_running_notes",
    "strip_banners",
    "validate_manifest",
    "validate_segmentation",
]
