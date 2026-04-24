"""Characterization tests for work_buddy.journal_backlog.clustering.

These tests document the expected behavior of the (substrate-agnostic)
clustering primitives — they operate on manifest entries with ``id``
and ``tags`` fields, regardless of which segmentation produced the
underlying threads.
"""

from __future__ import annotations

from typing import Any


def _entry(tid: str, tags: list[str], summary: str = "") -> dict[str, Any]:
    return {"id": tid, "tags": tags, "summary": summary}


def test_jaccard_similarity_basic() -> None:
    from work_buddy.journal_backlog.clustering import jaccard_similarity

    assert jaccard_similarity({"a", "b"}, {"b", "c"}) == 1 / 3
    assert jaccard_similarity({"a"}, {"a"}) == 1.0
    assert jaccard_similarity({"a"}, {"b"}) == 0.0
    # Both-empty is treated as a degenerate-equal case (1.0); one-empty
    # returns 0.0 because we can't share what isn't there.
    assert jaccard_similarity(set(), set()) == 1.0
    assert jaccard_similarity({"a"}, set()) == 0.0


def test_linearize_threads_groups_by_tag_overlap() -> None:
    from work_buddy.journal_backlog.clustering import linearize_threads

    entries = [
        _entry("t_0", ["tax-prep", "advisor"]),
        _entry("t_1", ["tax-prep", "deadline"]),
        _entry("t_2", ["etf-tracking"]),
    ]
    clusters = linearize_threads(entries, break_threshold=0.15)
    # Two entries share "tax-prep" → cluster together; the etf one stands alone.
    assert len(clusters) == 2
    cluster_ids = [{e["id"] for e in cluster} for cluster in clusters]
    assert {"t_0", "t_1"} in cluster_ids
    assert {"t_2"} in cluster_ids


def test_linearize_threads_break_threshold_high_separates_all() -> None:
    """Setting threshold above 1 forces every entry into its own cluster."""
    from work_buddy.journal_backlog.clustering import linearize_threads

    entries = [
        _entry("t_0", ["a", "b"]),
        _entry("t_1", ["a", "b"]),  # same tags as t_0
    ]
    clusters = linearize_threads(entries, break_threshold=2.0)
    assert len(clusters) == 2
    # Each cluster has exactly one entry.
    assert all(len(c) == 1 for c in clusters)


def test_generate_clustered_review_renders_markdown() -> None:
    from work_buddy.journal_backlog.clustering import generate_clustered_review

    threads = [
        {"id": "t_0", "raw_text": "first thread content",
         "line_count": 1, "source_dates": [], "has_multi_flag": False, "lines": [1]},
        {"id": "t_1", "raw_text": "second thread content",
         "line_count": 1, "source_dates": [], "has_multi_flag": False, "lines": [2]},
    ]
    manifest = [
        _entry("t_0", ["topic"], summary="First."),
        _entry("t_1", ["topic"], summary="Second."),
    ]
    md = generate_clustered_review(
        threads,
        manifest,
        journal_date="2026-04-24",
        source_dates=[],
        break_threshold=0.15,
    )
    assert isinstance(md, str)
    assert "t_0" in md
    assert "t_1" in md
    assert "## " in md  # has at least one cluster header at h2
