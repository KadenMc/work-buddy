"""Tests for work_buddy.journal_backlog.rewrite — line-range Running Notes rewrite.

The new rewrite operates on thread dicts that carry their own
``lines: list[int]`` (1-based, from the line-range segmentation path)
plus the original numbered lines, rather than stripping inline
``<!-- [t_xxx] -->`` annotations. Decision rules per thread come from a
``routing_record`` (one entry per thread id with an ``action`` field);
optional ``rewrite_map`` provides explicit replacement text for split
threads.

Test rules under verification:
- skip → keep the thread's lines.
- route / delete → drop the thread's lines.
- split → use ``rewrite_map[id]`` (string = replacement, None = drop);
  missing entry → log warning, treat as skip.
- Multi-thread overlap: a line is kept if ANY of its threads is in the
  keep-decision set; only dropped if ALL its memberships are drop-decisions.
- Unassigned lines (blanks / structural) are always kept.
- Consecutive blank lines collapse to one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# build_rewrite_preview — pure-Python, no I/O, no consent
# ---------------------------------------------------------------------------


def _thread(tid: str, lines: list[int], **extra: Any) -> dict[str, Any]:
    """Build a minimal thread dict matching build_threads_from_line_ranges output."""
    return {"id": tid, "lines": lines, "raw_text": "", "line_count": len(lines),
            "source_dates": [], "has_multi_flag": False, **extra}


def test_build_rewrite_preview_keeps_skipped_drops_routed() -> None:
    from work_buddy.journal_backlog.rewrite import build_rewrite_preview

    original = "alpha\nbeta\ngamma\ndelta"
    threads = [
        _thread("t_a", [1, 2]),
        _thread("t_b", [3, 4]),
    ]
    routing = {
        "items": [
            {"id": "t_a", "action": "skip"},
            {"id": "t_b", "action": "route"},
        ],
    }
    result = build_rewrite_preview(
        original_text=original, threads=threads, routing_record=routing,
    )
    assert "alpha" in result["rewritten_text"]
    assert "beta" in result["rewritten_text"]
    assert "gamma" not in result["rewritten_text"]
    assert "delta" not in result["rewritten_text"]
    assert result["kept_ids"] == ["t_a"]
    assert result["removed_ids"] == ["t_b"]


def test_build_rewrite_preview_preserves_unassigned_lines() -> None:
    """Lines outside any thread (blanks, separators, stray content) are kept
    regardless of decisions on the surrounding threads."""
    from work_buddy.journal_backlog.rewrite import build_rewrite_preview

    # Lines: 1=alpha (t_a), 2=blank (unassigned), 3=beta (t_b), 4=---  (unassigned)
    original = "alpha\n\nbeta\n---"
    threads = [
        _thread("t_a", [1]),
        _thread("t_b", [3]),
    ]
    routing = {
        "items": [
            {"id": "t_a", "action": "route"},
            {"id": "t_b", "action": "route"},
        ],
    }
    result = build_rewrite_preview(
        original_text=original, threads=threads, routing_record=routing,
    )
    # Both threads dropped — only the unassigned lines (blank, ---) remain.
    assert "alpha" not in result["rewritten_text"]
    assert "beta" not in result["rewritten_text"]
    assert "---" in result["rewritten_text"]


def test_build_rewrite_preview_overlap_kept_when_any_skipped() -> None:
    """A line in two threads where one is skip, one is route → line kept."""
    from work_buddy.journal_backlog.rewrite import build_rewrite_preview

    original = "alpha\nshared\nbeta"
    threads = [
        _thread("t_a", [1, 2]),     # skip
        _thread("t_b", [2, 3]),     # route
    ]
    routing = {
        "items": [
            {"id": "t_a", "action": "skip"},
            {"id": "t_b", "action": "route"},
        ],
    }
    result = build_rewrite_preview(
        original_text=original, threads=threads, routing_record=routing,
    )
    # Line 2 ("shared") is in both threads; t_a kept it → line 2 survives.
    assert "shared" in result["rewritten_text"]
    assert "alpha" in result["rewritten_text"]   # in skipped t_a
    assert "beta" not in result["rewritten_text"]  # only in routed t_b


def test_build_rewrite_preview_overlap_dropped_when_all_dropped() -> None:
    """Multi-thread line with all-drop memberships → line dropped."""
    from work_buddy.journal_backlog.rewrite import build_rewrite_preview

    original = "alpha\nshared\nbeta"
    threads = [
        _thread("t_a", [1, 2]),     # route
        _thread("t_b", [2, 3]),     # delete
    ]
    routing = {
        "items": [
            {"id": "t_a", "action": "route"},
            {"id": "t_b", "action": "delete"},
        ],
    }
    result = build_rewrite_preview(
        original_text=original, threads=threads, routing_record=routing,
    )
    assert "shared" not in result["rewritten_text"]


def test_build_rewrite_preview_split_with_explicit_map() -> None:
    """Split decision plus rewrite_map → original lines dropped, replacement
    text inserted at the original position."""
    from work_buddy.journal_backlog.rewrite import build_rewrite_preview

    original = "alpha\nbeta\ngamma\ndelta"
    threads = [
        _thread("t_split", [2, 3]),
    ]
    routing = {"items": [{"id": "t_split", "action": "split"}]}
    rewrite_map = {"t_split": "REPLACEMENT"}
    result = build_rewrite_preview(
        original_text=original,
        threads=threads,
        routing_record=routing,
        rewrite_map=rewrite_map,
    )
    assert "REPLACEMENT" in result["rewritten_text"]
    assert "beta" not in result["rewritten_text"]
    assert "gamma" not in result["rewritten_text"]
    # Surrounding lines preserved
    assert "alpha" in result["rewritten_text"]
    assert "delta" in result["rewritten_text"]


def test_build_rewrite_preview_split_without_map_warns_and_keeps(caplog) -> None:
    from work_buddy.journal_backlog.rewrite import build_rewrite_preview

    original = "alpha\nbeta"
    threads = [_thread("t_split", [1, 2])]
    routing = {"items": [{"id": "t_split", "action": "split"}]}

    with caplog.at_level(logging.WARNING, logger="work_buddy.journal_backlog.rewrite"):
        result = build_rewrite_preview(
            original_text=original, threads=threads, routing_record=routing,
        )

    warns = [r for r in caplog.records if "split" in r.getMessage().lower()]
    assert warns, "expected a warning about split without rewrite_map"
    # Conservative behavior: lines kept (treat as skip).
    assert "alpha" in result["rewritten_text"]
    assert "beta" in result["rewritten_text"]
    assert "t_split" not in result["removed_ids"]


def test_build_rewrite_preview_collapses_consecutive_blanks() -> None:
    from work_buddy.journal_backlog.rewrite import build_rewrite_preview

    # Multiple blank lines between content; some content dropped
    original = "alpha\n\n\nbeta\n\n\n\ngamma"
    threads = [_thread("t_a", [4]), _thread("t_b", [8])]
    routing = {
        "items": [
            {"id": "t_a", "action": "skip"},
            {"id": "t_b", "action": "skip"},
        ],
    }
    result = build_rewrite_preview(
        original_text=original, threads=threads, routing_record=routing,
    )
    out = result["rewritten_text"]
    # No run of 2 or more blank lines in the output.
    import re
    assert not re.search(r"\n\s*\n\s*\n", out), f"consecutive blanks not collapsed: {out!r}"


# ---------------------------------------------------------------------------
# rewrite_running_notes — file write, consent-gated
# ---------------------------------------------------------------------------


_VALID_FILE = """\
# Daily — 2026-04-24

## Some content

# **Running Notes / Considerations**

alpha
beta
gamma

# **Next section**

other content here
"""


def _grant_journal_consent(monkeypatch) -> None:
    """Force the rewrite consent gate to allow operations during the test."""
    from work_buddy.consent import grant_consent
    grant_consent("journal.rewrite_running_notes", mode="always")


def test_rewrite_running_notes_locates_section_and_writes(
    tmp_path: Path, monkeypatch,
) -> None:
    from work_buddy.journal_backlog.rewrite import rewrite_running_notes

    _grant_journal_consent(monkeypatch)

    journal = tmp_path / "2026-04-24.md"
    journal.write_text(_VALID_FILE, encoding="utf-8")

    # Running Notes section content (between header and next heading).
    # alpha=line 1, beta=line 2, gamma=line 3 of the section body.
    threads = [_thread("t_a", [1]), _thread("t_b", [3])]
    routing = {
        "items": [
            {"id": "t_a", "action": "skip"},
            {"id": "t_b", "action": "route"},
        ],
    }
    result = rewrite_running_notes(
        journal_path=journal,
        original_text="alpha\nbeta\ngamma",
        threads=threads,
        routing_record=routing,
        original_file_content=_VALID_FILE,
    )
    assert result["success"] is True
    new_content = journal.read_text(encoding="utf-8")
    # Sections before/after preserved
    assert "# Daily — 2026-04-24" in new_content
    assert "# **Next section**" in new_content
    assert "other content here" in new_content
    # Section body: alpha kept, gamma dropped
    assert "alpha" in new_content
    assert "gamma" not in new_content


def test_rewrite_running_notes_idempotent_when_all_skip(
    tmp_path: Path, monkeypatch,
) -> None:
    from work_buddy.journal_backlog.rewrite import rewrite_running_notes

    _grant_journal_consent(monkeypatch)

    journal = tmp_path / "2026-04-24.md"
    journal.write_text(_VALID_FILE, encoding="utf-8")

    threads = [_thread("t_a", [1, 2, 3])]
    routing = {"items": [{"id": "t_a", "action": "skip"}]}
    rewrite_running_notes(
        journal_path=journal,
        original_text="alpha\nbeta\ngamma",
        threads=threads,
        routing_record=routing,
        original_file_content=_VALID_FILE,
    )
    new_content = journal.read_text(encoding="utf-8")
    # All section content preserved
    assert "alpha" in new_content
    assert "beta" in new_content
    assert "gamma" in new_content


def test_rewrite_running_notes_is_consent_gated() -> None:
    """The rewrite capability is registered with the consent system at
    import time. We verify the registration here rather than the runtime
    block (which is order-dependent inside test sessions running under
    a workflow consent blanket)."""
    from work_buddy.consent import _CONSENT_REGISTRY
    from work_buddy.journal_backlog import rewrite_running_notes  # noqa: F401

    assert "journal.rewrite_running_notes" in _CONSENT_REGISTRY
    entry = _CONSENT_REGISTRY["journal.rewrite_running_notes"]
    assert entry["risk"] == "high"
    assert entry["reason"]  # non-empty rationale
