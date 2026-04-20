"""Unit tests for the journal triage adapter + repair_segmentation helper."""

from __future__ import annotations

from typing import Any

import pytest

from work_buddy.journal_backlog.segment import (
    generate_thread_ids,
    repair_segmentation,
    validate_segmentation,
)


# ---------------------------------------------------------------------------
# repair_segmentation
# ---------------------------------------------------------------------------


def test_repair_passthrough_on_valid_result() -> None:
    out = repair_segmentation(
        tagged_text="x",
        validation_result={"valid": True},
        original_text="x",
        id_pool=["t_aaaaaa"],
    )
    assert out["should_retry"] is False
    assert out["errors_grouped"] == {}
    assert "t_aaaaaa" in out["available_ids"]


def test_repair_categorizes_errors_and_lists_available_ids() -> None:
    pool = ["t_aaaaaa", "t_bbbbbb", "t_cccccc"]
    tagged = (
        "<!-- [t_aaaaaa] -->\n"
        "note line\n"
        "<!-- [/t_aaaaaa] -->\n"
    )
    validation = {
        "valid": False,
        "errors": [
            "Open tags without close: ['t_bbbbbb']",
            "Nested thread: t_cccccc opened inside t_aaaaaa at line 4",
            "Content modified at line 3: 'foo' -> 'bar'",
        ],
    }
    out = repair_segmentation(
        tagged_text=tagged,
        validation_result=validation,
        original_text="note line",
        id_pool=pool,
    )
    assert out["should_retry"] is True
    assert "unbalanced_tags" in out["errors_grouped"]
    assert "nesting" in out["errors_grouped"]
    assert "content_drift" in out["errors_grouped"]
    # t_aaaaaa was used in the attempt; bbbb/cccc were not.
    assert "t_aaaaaa" not in out["available_ids"]
    assert "t_bbbbbb" in out["available_ids"]
    assert "t_cccccc" in out["available_ids"]
    assert "byte-for-byte" in out["instructions"]


def test_repair_refuses_retry_on_heavy_drift() -> None:
    validation = {
        "valid": False,
        "errors": [f"Content modified at line {i}: 'a' -> 'b'" for i in range(6)],
    }
    out = repair_segmentation(
        tagged_text="",
        validation_result=validation,
        original_text="original",
        id_pool=["t_aaaaaa"],
    )
    assert out["should_retry"] is False


# ---------------------------------------------------------------------------
# validate_segmentation ↔ repair_segmentation round-trip
# ---------------------------------------------------------------------------


def test_repair_respects_validate_output() -> None:
    """End-to-end: a truly-broken attempt's validate output feeds cleanly
    into repair_segmentation without attribute errors."""
    original = "one line\ntwo line"
    bad_tagged = (
        "<!-- [t_aaaaaa] -->\n"
        "one line\n"
        "<!-- [t_bbbbbb] -->\n"
        "two line\n"
        # Missing close tags entirely
    )
    result = validate_segmentation(bad_tagged, original)
    assert result["valid"] is False
    repair = repair_segmentation(
        tagged_text=bad_tagged,
        validation_result=result,
        original_text=original,
        id_pool=generate_thread_ids(5),
    )
    # Helper should produce something usable regardless of our exact
    # error grouping
    assert isinstance(repair["instructions"], str)
    assert isinstance(repair["available_ids"], list)


# ---------------------------------------------------------------------------
# Journal adapter
# ---------------------------------------------------------------------------


def test_journal_adapter_returns_empty_when_no_notes(monkeypatch) -> None:
    """No running-notes text → empty items, no hash."""
    from work_buddy.triage.adapters import journal as adapter_mod

    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes",
        lambda **kw: "",
    )
    items, ch = adapter_mod.collect_same_day_candidates(
        journal_date="2026-04-18", profile="local_general",
    )
    assert items == []
    assert ch is None


def test_journal_adapter_returns_empty_when_read_raises(monkeypatch) -> None:
    from work_buddy.triage.adapters import journal as adapter_mod

    def boom(**kw: Any) -> str:
        raise RuntimeError("vault down")

    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes", boom,
    )
    items, ch = adapter_mod.collect_same_day_candidates(
        journal_date="2026-04-18", profile="local_general",
    )
    assert items == []
    assert ch is None


def test_journal_adapter_empty_when_segmentation_fails(monkeypatch) -> None:
    """Read returns content; segmenter returns unparseable output → [] ."""
    from work_buddy.triage.adapters import journal as adapter_mod

    notes = "- alpha\n- beta\n- gamma"
    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes",
        lambda **kw: notes,
    )
    # LLMRunner returns content that doesn't parse as valid segmentation JSON.
    from work_buddy.llm import LLMResponse
    monkeypatch.setattr(
        "work_buddy.llm.runner_v2.LLMRunner.call",
        lambda self, **kw: LLMResponse(content="nothing sensible"),
    )
    items, ch = adapter_mod.collect_same_day_candidates(
        journal_date="2026-04-18", profile="local_general",
    )
    assert items == []
    # Hash present because we did see content, even though segmentation failed
    assert ch is not None


def test_journal_adapter_builds_items_from_valid_segmentation(
    monkeypatch,
) -> None:
    """Give the adapter a segmenter that returns a valid line-range
    JSON mapping and confirm TriageItems are produced with the right
    shape."""
    from work_buddy.triage.adapters import journal as adapter_mod

    original = "- alpha idea\n- beta idea"
    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes",
        lambda **kw: original,
    )

    # Force a deterministic id pool so we can assemble a valid response.
    fixed_ids = ["t_aaaaaa", "t_bbbbbb"] + [
        f"t_{i:06x}" for i in range(62)
    ]
    monkeypatch.setattr(
        "work_buddy.journal_backlog.segment.generate_thread_ids",
        lambda count=50: list(fixed_ids[:count]),
    )

    # Line-range JSON: line 1 → t_aaaaaa, line 2 → t_bbbbbb
    import json as _json
    payload = _json.dumps({
        "threads": [
            {"id": "t_aaaaaa", "lines": [1]},
            {"id": "t_bbbbbb", "lines": [2]},
        ],
    })
    from work_buddy.llm import LLMResponse
    monkeypatch.setattr(
        "work_buddy.llm.runner_v2.LLMRunner.call",
        lambda self, **kw: LLMResponse(content=payload),
    )

    items, ch = adapter_mod.collect_same_day_candidates(
        journal_date="2026-04-18", profile="local_general",
    )
    assert ch is not None
    assert len(items) == 2
    ids = {i.id for i in items}
    assert ids == {"journal_t_aaaaaa", "journal_t_bbbbbb"}
    for item in items:
        assert item.source == "journal_thread"
        assert item.label  # non-empty
        assert item.metadata["journal_date"] == "2026-04-18"
    # Reconstructed raw_text for t_aaaaaa should be exactly line 1 content
    alpha = next(i for i in items if i.id == "journal_t_aaaaaa")
    assert alpha.text == "- alpha idea"


def test_journal_adapter_repairs_then_succeeds(monkeypatch) -> None:
    """First call has missing coverage; repair retry fills it in."""
    from work_buddy.triage.adapters import journal as adapter_mod

    original = "- alpha idea\n- beta idea"
    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes",
        lambda **kw: original,
    )
    fixed_ids = ["t_aaaaaa", "t_bbbbbb"] + [
        f"t_{i:06x}" for i in range(62)
    ]
    monkeypatch.setattr(
        "work_buddy.journal_backlog.segment.generate_thread_ids",
        lambda count=50: list(fixed_ids[:count]),
    )

    # First response: only covers line 1 (line 2 unassigned → fails)
    import json as _json
    first = _json.dumps({"threads": [{"id": "t_aaaaaa", "lines": [1]}]})
    # Second (repair) response: covers both lines
    second = _json.dumps({
        "threads": [
            {"id": "t_aaaaaa", "lines": [1]},
            {"id": "t_bbbbbb", "lines": [2]},
        ],
    })
    responses = iter([first, second])

    from work_buddy.llm import LLMResponse

    def fake_llm_call(self, **kw):
        return LLMResponse(content=next(responses))

    monkeypatch.setattr("work_buddy.llm.runner_v2.LLMRunner.call", fake_llm_call)

    items, ch = adapter_mod.collect_same_day_candidates(
        journal_date="2026-04-18", profile="local_general",
    )
    assert ch is not None
    assert len(items) == 2


# ---------------------------------------------------------------------------
# Line-range segmentation primitives
# ---------------------------------------------------------------------------


def test_number_lines_format() -> None:
    from work_buddy.journal_backlog.segment import number_lines

    numbered, originals = number_lines("first\nsecond\n\nfourth")
    assert numbered == "1| first\n2| second\n3| \n4| fourth"
    assert originals == ["first", "second", "", "fourth"]


def test_validate_line_range_accepts_well_formed() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- one", "- two", "- three"]
    seg = {
        "threads": [
            {"id": "t_aaaaaa", "lines": [1, 2]},
            {"id": "t_bbbbbb", "lines": [3]},
        ],
    }
    result = validate_line_range_segmentation(
        seg, originals, id_pool=["t_aaaaaa", "t_bbbbbb"],
    )
    assert result["valid"] is True
    assert result["thread_count"] == 2


def test_validate_line_range_rejects_missing_coverage() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- one", "- two", "- three"]
    seg = {"threads": [{"id": "t_aaaaaa", "lines": [1]}]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("not assigned" in e for e in result["errors"])


def test_validate_line_range_rejects_overlap_without_multi() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- one", "- two"]
    seg = {
        "threads": [
            {"id": "t_aaaaaa", "lines": [1, 2]},
            {"id": "t_bbbbbb", "lines": [2]},
        ],
    }
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("cited by" in e and "multi" in e for e in result["errors"])


def test_validate_line_range_allows_overlap_with_multi() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- shared", "- only-b"]
    seg = {
        "threads": [
            {"id": "t_aaaaaa", "lines": [1], "multi": True},
            {"id": "t_bbbbbb", "lines": [1, 2], "multi": True},
        ],
    }
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True


def test_validate_line_range_rejects_bad_ids_and_oor_lines() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b"]
    seg = {
        "threads": [
            {"id": "bad_id", "lines": [1]},
            {"id": "t_aaaaaa", "lines": [5]},
        ],
    }
    result = validate_line_range_segmentation(
        seg, originals, id_pool=["t_aaaaaa", "t_bbbbbb"],
    )
    assert result["valid"] is False
    errs = " ".join(result["errors"])
    assert "invalid id" in errs.lower()
    assert "out of range" in errs.lower()


def test_build_threads_from_line_ranges_reconstructs_raw_text() -> None:
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        validate_line_range_segmentation,
    )

    originals = ["- alpha", "- beta", "- gamma"]
    seg = {
        "threads": [
            {"id": "t_aaaaaa", "lines": [1, 2]},
            {"id": "t_bbbbbb", "lines": [3]},
        ],
    }
    validated = validate_line_range_segmentation(seg, originals)
    assert validated["valid"]
    threads = build_threads_from_line_ranges(validated, originals)
    by_id = {t["id"]: t for t in threads}
    assert by_id["t_aaaaaa"]["raw_text"] == "- alpha\n- beta"
    assert by_id["t_aaaaaa"]["line_count"] == 2
    assert by_id["t_bbbbbb"]["raw_text"] == "- gamma"


def test_validate_line_range_permits_unassigned_separator() -> None:
    """Standalone `---` lines carry boundary info, not content. The
    validator must NOT require them to be assigned to a thread, so a
    trailing separator doesn't force the model to spawn a junk thread
    around it."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- alpha idea", "- beta idea", "---"]
    seg = {
        "threads": [
            {"id": "t_aaaaaa", "lines": [1]},
            {"id": "t_bbbbbb", "lines": [2]},
        ],
    }
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True
    # Line 3 (`---`) was legitimately skipped — no error.
    assert all("not assigned" not in e for e in result["errors"])


def test_repair_line_range_extracts_available_ids() -> None:
    from work_buddy.journal_backlog.segment import (
        repair_line_range_segmentation,
        validate_line_range_segmentation,
    )

    originals = ["- one", "- two"]
    pool = ["t_aaaaaa", "t_bbbbbb", "t_cccccc"]
    seg = {"threads": [{"id": "t_aaaaaa", "lines": [1]}]}
    validation = validate_line_range_segmentation(seg, originals, id_pool=pool)
    repair = repair_line_range_segmentation(
        segmentation=seg,
        validation_result=validation,
        original_lines=originals,
        id_pool=pool,
    )
    assert repair["should_retry"] is True
    assert "t_aaaaaa" not in repair["available_ids"]
    assert "t_bbbbbb" in repair["available_ids"]
    assert "missing_coverage" in repair["errors_grouped"]
