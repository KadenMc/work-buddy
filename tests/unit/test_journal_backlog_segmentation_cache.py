"""Tests for work_buddy.journal_backlog.segmentation_cache.

The segmentation cache is content-addressable: it stores groups as sets
of per-line content hashes (not line numbers) so a re-run on the same
content — even with different line numbering or reordering — can reuse
the result and emit groups translated to the new line numbers.

Misses on any meaningful content change (line added, removed, modified
beyond whitespace). System-prompt edits invalidate via the
``system_hash`` scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "segmentation.json"


# ---------------------------------------------------------------------------
# Round-trip / full hit
# ---------------------------------------------------------------------------


def test_cache_full_hit_round_trip(cache_path: Path) -> None:
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    lines = ["- alpha", "- beta", "- gamma"]
    groups = [[1, 2], [3]]
    put_segmentation(
        original_lines=lines, system_hash="sys1",
        groups=groups, cache_path=cache_path,
    )
    hit = get_cached_segmentation(
        original_lines=lines, system_hash="sys1", cache_path=cache_path,
    )
    assert hit == [[1, 2], [3]]


def test_cache_robust_to_line_reordering(cache_path: Path) -> None:
    """Same lines in a different order: the cache should still hit and
    emit groups translated to the new line positions."""
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    original = ["- alpha", "- beta", "- gamma"]
    put_segmentation(
        original_lines=original, system_hash="sys1",
        groups=[[1, 2], [3]],   # alpha+beta together, gamma alone
        cache_path=cache_path,
    )
    # Reordered: gamma is now line 1, alpha line 2, beta line 3.
    reordered = ["- gamma", "- alpha", "- beta"]
    hit = get_cached_segmentation(
        original_lines=reordered, system_hash="sys1", cache_path=cache_path,
    )
    # Expected groups translated by content:
    #   alpha+beta cluster → now lines [2, 3]
    #   gamma cluster → now line [1]
    assert hit is not None
    sorted_groups = sorted(sorted(g) for g in hit)
    assert sorted_groups == [[1], [2, 3]]


def test_cache_robust_to_whitespace_normalization(cache_path: Path) -> None:
    """Trivial whitespace-only line edits should not break the cache."""
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    put_segmentation(
        original_lines=["- alpha", "- beta"],
        system_hash="sys1", groups=[[1, 2]], cache_path=cache_path,
    )
    # Same content but with extra whitespace.
    hit = get_cached_segmentation(
        original_lines=["-  alpha  ", "- beta"],
        system_hash="sys1", cache_path=cache_path,
    )
    assert hit == [[1, 2]]


def test_cache_blank_lines_ignored(cache_path: Path) -> None:
    """Adding/removing blank lines or separators doesn't change the
    line-content set — those are structural, not content."""
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    put_segmentation(
        original_lines=["- alpha", "- beta"],
        system_hash="sys1", groups=[[1, 2]], cache_path=cache_path,
    )
    # New input has same content but with blanks and separators interleaved.
    hit = get_cached_segmentation(
        original_lines=["", "- alpha", "---", "- beta", ""],
        system_hash="sys1", cache_path=cache_path,
    )
    assert hit is not None
    # alpha → new line 2, beta → new line 4
    sorted_groups = sorted(sorted(g) for g in hit)
    assert sorted_groups == [[2, 4]]


# ---------------------------------------------------------------------------
# Miss conditions
# ---------------------------------------------------------------------------


def test_cache_miss_on_added_content_line(cache_path: Path) -> None:
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    put_segmentation(
        original_lines=["- alpha", "- beta"],
        system_hash="sys1", groups=[[1, 2]], cache_path=cache_path,
    )
    # New non-blank line added.
    hit = get_cached_segmentation(
        original_lines=["- alpha", "- beta", "- gamma"],
        system_hash="sys1", cache_path=cache_path,
    )
    assert hit is None


def test_cache_miss_on_removed_content_line(cache_path: Path) -> None:
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    put_segmentation(
        original_lines=["- alpha", "- beta", "- gamma"],
        system_hash="sys1", groups=[[1, 2, 3]], cache_path=cache_path,
    )
    hit = get_cached_segmentation(
        original_lines=["- alpha", "- beta"],
        system_hash="sys1", cache_path=cache_path,
    )
    assert hit is None


def test_cache_miss_on_changed_line_content(cache_path: Path) -> None:
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    put_segmentation(
        original_lines=["- alpha", "- beta"],
        system_hash="sys1", groups=[[1, 2]], cache_path=cache_path,
    )
    # One line's content changed substantively.
    hit = get_cached_segmentation(
        original_lines=["- alpha", "- BETA-DIFFERENT"],
        system_hash="sys1", cache_path=cache_path,
    )
    assert hit is None


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def test_cache_isolated_by_system_hash(cache_path: Path) -> None:
    """Same content, different system_hash → miss. Editing the system
    prompt cleanly invalidates."""
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    put_segmentation(
        original_lines=["- alpha", "- beta"],
        system_hash="sys1", groups=[[1, 2]], cache_path=cache_path,
    )
    miss = get_cached_segmentation(
        original_lines=["- alpha", "- beta"],
        system_hash="sys2_different_prompt", cache_path=cache_path,
    )
    assert miss is None


def test_cache_expires_via_ttl(cache_path: Path) -> None:
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    put_segmentation(
        original_lines=["- alpha"],
        system_hash="sys1", groups=[[1]],
        ttl_minutes=0, cache_path=cache_path,  # immediately expired
    )
    hit = get_cached_segmentation(
        original_lines=["- alpha"],
        system_hash="sys1", cache_path=cache_path,
    )
    assert hit is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_cache_handles_duplicate_line_content(cache_path: Path) -> None:
    """Two lines with identical text: when caching, the line set has one
    entry; on lookup, duplicate-text new input maps to a single group
    membership for both line numbers (correct: identical lines belong
    to the same thread)."""
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    # Cache stores 2 unique-content lines.
    put_segmentation(
        original_lines=["- shared", "- other"],
        system_hash="sys1", groups=[[1], [2]], cache_path=cache_path,
    )
    # New input has the same lines but "- shared" repeats.
    hit = get_cached_segmentation(
        original_lines=["- shared", "- other", "- shared"],
        system_hash="sys1", cache_path=cache_path,
    )
    # Different line set → miss (extra non-blank line).
    assert hit is None


def test_cache_returns_none_when_file_missing(tmp_path: Path) -> None:
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
    )

    hit = get_cached_segmentation(
        original_lines=["- alpha"], system_hash="sys1",
        cache_path=tmp_path / "nonexistent.json",
    )
    assert hit is None
