"""Unit tests for the journal triage adapter and line-range segmentation.

Covers:
- ``collect_same_day_candidates`` happy path + empty/error edges
- Escalation loop: validation failure at one tier → retry at next
- Exhaustion audit log shape
- ``validate_line_range_segmentation`` (new ``{"groups": [[...]]}`` shape)
- ``build_threads_from_line_ranges`` (local id assignment + multi detection)
"""

from __future__ import annotations

from typing import Any

from work_buddy.llm import ModelTier


# ---------------------------------------------------------------------------
# Adapter: collect_same_day_candidates
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
    """Model returns valid line-range groups → TriageItems produced with
    locally-generated ids and correct raw_text."""
    from work_buddy.triage.adapters import journal as adapter_mod

    original = "- alpha idea\n- beta idea"
    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes",
        lambda **kw: original,
    )

    import json as _json
    payload = _json.dumps({"groups": [[1], [2]]})
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
    for item in items:
        assert item.source == "journal_thread"
        assert item.label  # non-empty
        assert item.metadata["journal_date"] == "2026-04-18"
        assert item.id.startswith("journal_t_")
    # Raw text reconstruction is line-accurate.
    texts = {item.text for item in items}
    assert texts == {"- alpha idea", "- beta idea"}


def test_journal_adapter_escalates_on_validation_failure(monkeypatch) -> None:
    """First tier produces invalid content (missing coverage); escalation
    to the next tier produces valid content."""
    from work_buddy.triage.adapters import journal as adapter_mod

    original = "- alpha idea\n- beta idea"
    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes",
        lambda **kw: original,
    )

    import json as _json
    # First tier covers only line 1 (line 2 unassigned → fails)
    first = _json.dumps({"groups": [[1]]})
    # Second tier covers both lines
    second = _json.dumps({"groups": [[1], [2]]})
    responses = iter([first, second])
    tiers_called: list[ModelTier] = []

    from work_buddy.llm import LLMResponse

    def fake_llm_call(self, *, tier, **kw):
        tiers_called.append(tier)
        return LLMResponse(content=next(responses))

    monkeypatch.setattr("work_buddy.llm.runner_v2.LLMRunner.call", fake_llm_call)

    items, ch = adapter_mod.collect_same_day_candidates(
        journal_date="2026-04-18",
        profile="local_general",
        tier_chain=[ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST],
    )
    assert ch is not None
    assert len(items) == 2
    assert tiers_called == [ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST]


def test_journal_adapter_exhausts_tier_chain_and_logs_audit(
    monkeypatch, caplog,
) -> None:
    """Every tier fails content validation → empty items + audit log."""
    import logging
    from work_buddy.triage.adapters import journal as adapter_mod

    original = "- alpha idea\n- beta idea"
    monkeypatch.setattr(
        "work_buddy.journal_backlog.read_running_notes",
        lambda **kw: original,
    )

    # Both tiers return invalid (line 2 unassigned)
    import json as _json
    bad = _json.dumps({"groups": [[1]]})

    from work_buddy.llm import LLMResponse
    monkeypatch.setattr(
        "work_buddy.llm.runner_v2.LLMRunner.call",
        lambda self, **kw: LLMResponse(content=bad),
    )

    with caplog.at_level(
        logging.INFO, logger="work_buddy.triage.adapters.journal",
    ):
        items, ch = adapter_mod.collect_same_day_candidates(
            journal_date="2026-04-18",
            profile="local_general",
            tier_chain=[ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST],
        )

    assert items == []
    assert ch is not None
    audit_records = [
        r for r in caplog.records
        if "segmentation failed across all tiers" in r.getMessage()
    ]
    assert audit_records, "expected an exhaustion log line"
    msg = audit_records[0].getMessage()
    assert "local_fast" in msg
    assert "frontier_fast" in msg
    assert "missing_coverage" in msg


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
    seg = {"groups": [[1, 2], [3]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True
    assert result["group_count"] == 2
    assert result["groups"] == [[1, 2], [3]]


def test_validate_line_range_rejects_missing_coverage() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- one", "- two", "- three"]
    seg = {"groups": [[1]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("not assigned" in e for e in result["errors"])


def test_validate_line_range_allows_overlap_unconditionally() -> None:
    """Overlap between groups is allowed — no flag required. The multi-
    thread signal is encoded by the overlap itself."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- shared", "- only-b"]
    seg = {"groups": [[1], [1, 2]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True


def test_validate_line_range_accepts_any_well_formed_ids_freely() -> None:
    """Regression: the old validator rejected ids that weren't in a
    pre-generated pool. The new shape has no ids at all — groups are
    just line-number lists — so any well-formed partition passes."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b", "- c"]
    # 20 arbitrary groupings; as long as coverage is complete, each is valid.
    seg = {"groups": [[1, 2], [3]]}
    assert validate_line_range_segmentation(seg, originals)["valid"]
    seg = {"groups": [[1], [2, 3]]}
    assert validate_line_range_segmentation(seg, originals)["valid"]
    seg = {"groups": [[1, 2, 3]]}
    assert validate_line_range_segmentation(seg, originals)["valid"]


def test_validate_line_range_rejects_oor_line() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b"]
    seg = {"groups": [[1], [5]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("out of range" in e.lower() for e in result["errors"])


def test_validate_line_range_rejects_missing_groups_key() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a"]
    result = validate_line_range_segmentation({"threads": []}, originals)
    assert result["valid"] is False
    assert any("groups" in e.lower() for e in result["errors"])


def test_validate_line_range_rejects_non_numeric_string() -> None:
    """A string that isn't a number or range (``"two"``) is unparseable —
    caught distinctly from bad types like dicts or ``None``."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b"]
    seg = {"groups": [[1, "two"]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("unparseable" in e.lower() for e in result["errors"])


def test_validate_line_range_rejects_structurally_non_integer_type() -> None:
    """Dicts, lists, ``None`` aren't valid line entries at all."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b"]
    seg = {"groups": [[1, {"line": 2}]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("non-integer" in e.lower() for e in result["errors"])


def test_build_threads_from_line_ranges_reconstructs_raw_text() -> None:
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        validate_line_range_segmentation,
    )

    originals = ["- alpha", "- beta", "- gamma"]
    seg = {"groups": [[1, 2], [3]]}
    validated = validate_line_range_segmentation(seg, originals)
    assert validated["valid"]
    threads = build_threads_from_line_ranges(validated, originals)
    assert len(threads) == 2
    # Ids are locally generated t_xxxxxx — shape is stable but value is random.
    for t in threads:
        assert t["id"].startswith("t_") and len(t["id"]) == 8
    # Order preserved from the model's groups list.
    assert threads[0]["raw_text"] == "- alpha\n- beta"
    assert threads[0]["line_count"] == 2
    assert threads[1]["raw_text"] == "- gamma"
    assert threads[1]["line_count"] == 1


def test_build_threads_computes_multi_from_overlap() -> None:
    """has_multi_flag is True for any group whose lines also appear in
    another group."""
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        validate_line_range_segmentation,
    )

    originals = ["- shared", "- only-a", "- only-b"]
    # Group 1: {1, 2}, Group 2: {1, 3} — line 1 overlaps, so both multi.
    seg = {"groups": [[1, 2], [1, 3]]}
    validated = validate_line_range_segmentation(seg, originals)
    threads = build_threads_from_line_ranges(validated, originals)
    assert all(t["has_multi_flag"] for t in threads)


def test_build_threads_no_multi_when_groups_disjoint() -> None:
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b", "- c"]
    seg = {"groups": [[1, 2], [3]]}
    validated = validate_line_range_segmentation(seg, originals)
    threads = build_threads_from_line_ranges(validated, originals)
    assert all(not t["has_multi_flag"] for t in threads)


def test_build_threads_assigns_unique_ids() -> None:
    """Local id generation must produce unique ids within a run."""
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 11)]
    seg = {"groups": [[i] for i in range(1, 11)]}
    validated = validate_line_range_segmentation(seg, originals)
    threads = build_threads_from_line_ranges(validated, originals)
    ids = [t["id"] for t in threads]
    assert len(set(ids)) == len(ids), "ids must be unique within a run"


def test_validate_line_range_permits_unassigned_separator() -> None:
    """Standalone `---` lines carry boundary info, not content. The
    validator must NOT require them to be assigned to a group."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- alpha idea", "- beta idea", "---"]
    seg = {"groups": [[1], [2]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True
    assert all("not assigned" not in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Range / string entry parsing
# ---------------------------------------------------------------------------


def test_validate_line_range_accepts_inclusive_range_string() -> None:
    """``"3-5"`` expands to lines 3, 4, 5."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 6)]
    seg = {"groups": [[1, 2], ["3-5"]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True
    assert result["groups"] == [[1, 2], [3, 4, 5]]


def test_validate_line_range_accepts_mixed_int_and_range() -> None:
    """Entries in a single group can mix plain integers and range strings."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 11)]
    seg = {"groups": [[1, "3-5", 9], ["2", "6-8", 10]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True
    assert result["groups"] == [[1, 3, 4, 5, 9], [2, 6, 7, 8, 10]]


def test_validate_line_range_accepts_single_int_as_string() -> None:
    """Robustness: a model emitting ``"15"`` instead of ``15`` is accepted
    as line 15 — we must not fail just because the model wavered on the
    output type."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 21)]
    seg = {"groups": [["1", "2", "15"], [3, "4", 5]]}
    result = validate_line_range_segmentation(seg, originals)
    # Coverage is incomplete (many lines missing) — but the entries that
    # WERE given must parse cleanly; the only error should be coverage.
    parse_errors = [
        e for e in result["errors"]
        if "unparseable" in e or "non-integer" in e or "range" in e.lower()
    ]
    assert not parse_errors, f"expected string ints to parse, got: {parse_errors}"


def test_validate_line_range_accepts_degenerate_range() -> None:
    """``"5-5"`` is a valid single-line range (equivalent to ``5`` or ``"5"``)."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b", "- c"]
    seg = {"groups": [["1-1"], ["2-2"], ["3-3"]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True
    assert result["groups"] == [[1], [2], [3]]


def test_validate_line_range_tolerates_whitespace_in_range() -> None:
    """Model wobble: ``" 3 - 5 "`` should parse the same as ``"3-5"``."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 6)]
    seg = {"groups": [[1, 2], [" 3 - 5 "]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is True
    assert result["groups"] == [[1, 2], [3, 4, 5]]


def test_validate_line_range_rejects_reversed_range() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 6)]
    seg = {"groups": [["5-3"]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("reversed" in e.lower() for e in result["errors"])


def test_validate_line_range_rejects_zero_or_below() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 6)]
    # Zero-start range — 1-indexed input should reject.
    seg = {"groups": [["0-3"]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("below 1" in e.lower() for e in result["errors"])


def test_validate_line_range_rejects_range_exceeding_input() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 6)]  # 5 lines
    seg = {"groups": [["1-100"]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    # One error per bad range — not 95 errors per out-of-bounds line.
    range_errors = [e for e in result["errors"] if "1-100" in e]
    assert len(range_errors) == 1


def test_validate_line_range_rejects_unparseable_string() -> None:
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = [f"- line {i}" for i in range(1, 6)]
    seg = {"groups": [[1, "abc"]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("unparseable" in e.lower() for e in result["errors"])


def test_validate_line_range_rejects_bool_entry() -> None:
    """``True`` is a Python ``int`` subclass; accepting it silently would
    silently alias line 1. Reject explicitly."""
    from work_buddy.journal_backlog.segment import (
        validate_line_range_segmentation,
    )

    originals = ["- a", "- b"]
    seg = {"groups": [[True, 2]]}
    result = validate_line_range_segmentation(seg, originals)
    assert result["valid"] is False
    assert any("boolean" in e.lower() for e in result["errors"])


def test_build_threads_from_range_reconstructs_raw_text() -> None:
    """Range entries flow through the builder the same as integer lists."""
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        validate_line_range_segmentation,
    )

    originals = ["- alpha", "- beta", "- gamma", "- delta", "- epsilon"]
    seg = {"groups": [["1-3"], [4, 5]]}
    validated = validate_line_range_segmentation(seg, originals)
    assert validated["valid"]
    threads = build_threads_from_line_ranges(validated, originals)
    assert threads[0]["raw_text"] == "- alpha\n- beta\n- gamma"
    assert threads[0]["line_count"] == 3
    assert threads[1]["raw_text"] == "- delta\n- epsilon"
