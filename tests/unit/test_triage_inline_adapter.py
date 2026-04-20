"""Unit tests for the inline-selection triage adapter."""

from __future__ import annotations

from work_buddy.triage.adapters.inline import collect_inline_selection


def test_empty_inputs_return_empty() -> None:
    items, ch = collect_inline_selection(
        file_path="Notes/x.md",
        selection="",
        paragraph="",
        cursor_line=0,
        hint="",
    )
    assert items == []
    assert ch is None


def test_selection_becomes_item_with_metadata() -> None:
    items, ch = collect_inline_selection(
        file_path="Notes/Example.md",
        selection="Reach out to Bob about the manuscript.",
        paragraph="Reach out to Bob about the manuscript.\nAlso mention the plot.",
        cursor_line=17,
        hint="follow up",
    )
    assert ch is not None
    assert len(items) == 1
    item = items[0]
    assert item.source == "inline"
    assert item.id.startswith("inline_")
    assert item.text == "Reach out to Bob about the manuscript."
    # Label comes from hint (label_seed prefers hint over selection)
    assert "follow up" in item.label.lower()
    assert item.metadata["file_path"] == "Notes/Example.md"
    assert item.metadata["cursor_line"] == 17
    assert item.metadata["hint"] == "follow up"
    assert item.metadata["paragraph"].startswith("Reach out to Bob")


def test_falls_back_to_paragraph_when_no_selection() -> None:
    items, ch = collect_inline_selection(
        file_path="Notes/x.md",
        selection="",
        paragraph="Paragraph content that should be used.",
        cursor_line=3,
        hint="",
    )
    assert ch is not None
    assert len(items) == 1
    assert items[0].text == "Paragraph content that should be used."


def test_paragraph_metadata_is_truncated() -> None:
    long_para = "x" * 1000
    items, _ = collect_inline_selection(
        file_path="x.md",
        selection="short sel",
        paragraph=long_para,
        cursor_line=0,
        hint="",
    )
    assert len(items[0].metadata["paragraph"]) == 500


def test_identical_inputs_produce_identical_hash_and_id() -> None:
    a, ch_a = collect_inline_selection(
        file_path="f.md", selection="same", paragraph="", cursor_line=1, hint="",
    )
    b, ch_b = collect_inline_selection(
        file_path="f.md", selection="same", paragraph="", cursor_line=99, hint="different",
    )
    assert ch_a == ch_b
    assert a[0].id == b[0].id
