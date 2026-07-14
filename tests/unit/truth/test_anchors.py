from __future__ import annotations

import json

import pytest

from work_buddy.truth.anchors import (
    CompositeSelector,
    ResolvedAnchor,
    parse_selector,
    reanchor,
    serialize_selector,
    validate_position,
)
from work_buddy.truth.contracts import AnchorError
from work_buddy.truth.identity import sha256_text


def test_reanchor_resolves_an_exact_quote() -> None:
    text = "Start with the exact quoted passage and finish."
    selector = CompositeSelector(exact="exact quoted passage")

    assert reanchor(text, selector) == ResolvedAnchor(
        start=15,
        end=35,
        exact="exact quoted passage",
    )


def test_reanchor_tolerates_internal_whitespace_reflow() -> None:
    text = "Before alpha\n   beta after"
    selector = CompositeSelector(
        exact="alpha beta",
        prefix="Before ",
        suffix=" after",
    )

    resolved = reanchor(text, selector)

    assert resolved.start == 7
    assert resolved.end == 20
    assert resolved.exact == "alpha\n   beta"


def test_reanchor_uses_context_to_disambiguate_all_quote_candidates() -> None:
    text = "First target here. Second target there."
    selector = CompositeSelector(
        exact="target",
        prefix="Second ",
        suffix=" there",
    )

    resolved = reanchor(text, selector)

    assert resolved == ResolvedAnchor(26, 32, "target")


def test_reanchor_refuses_an_ambiguous_quote() -> None:
    text = "target then target"

    with pytest.raises(AnchorError, match=r"ambiguous.*0:6, 12:18"):
        reanchor(text, CompositeSelector(exact="target"))


def test_reanchor_refuses_a_missing_quote() -> None:
    with pytest.raises(AnchorError, match="anchor quote was not found"):
        reanchor("source text", CompositeSelector(exact="invented quote"))


def test_reanchor_checks_immutable_snapshot_hash_before_resolving() -> None:
    selector = CompositeSelector(exact="source")

    with pytest.raises(AnchorError, match="snapshot SHA-256 mismatch"):
        reanchor(
            "changed source",
            selector,
            expected_snapshot_sha256=sha256_text("original source"),
        )


def test_position_offsets_are_unicode_code_points() -> None:
    text = "🙂 café ready"
    selector = CompositeSelector(exact="café", start=2, end=6)

    expected = ResolvedAnchor(start=2, end=6, exact="café")
    assert validate_position(text, selector) == expected
    assert reanchor(
        text,
        selector,
        expected_snapshot_sha256=sha256_text(text),
    ) == expected


def test_position_selector_requires_the_exact_quote_at_its_offsets() -> None:
    selector = CompositeSelector(exact="café", start=0, end=4)

    with pytest.raises(AnchorError, match="quote mismatch"):
        validate_position("🙂 café ready", selector)


def test_selector_json_round_trip_uses_web_annotation_types() -> None:
    selector = CompositeSelector(
        exact="quoted text",
        prefix="before ",
        suffix=" after",
        start=7,
        end=18,
    )

    raw = serialize_selector(selector)
    data = json.loads(raw)

    assert data == [
        {
            "exact": "quoted text",
            "prefix": "before ",
            "suffix": " after",
            "type": "TextQuoteSelector",
        },
        {"end": 18, "start": 7, "type": "TextPositionSelector"},
    ]
    assert parse_selector(raw) == selector
    assert CompositeSelector.from_json(selector.to_json()) == selector


def test_selector_rejects_byte_counted_position_range() -> None:
    with pytest.raises(AnchorError, match="Unicode code-point length"):
        CompositeSelector(exact="café", start=2, end=7)
