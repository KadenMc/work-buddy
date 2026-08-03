from __future__ import annotations

from types import SimpleNamespace

from work_buddy.cowork.proposal_applicability import (
    CurrentProjection,
    assess_proposal_applicability,
)
from work_buddy.truth.anchors import CompositeSelector


def _proposal(*, base: str, head: str | None, exact: str):
    return SimpleNamespace(
        base_content_sha256=base,
        base_structured_head_sha256=head,
        selector_json=CompositeSelector(exact=exact).to_json(),
    )


def _projection(text: str, *, head: str = "b" * 64) -> CurrentProjection:
    return CurrentProjection(
        text=text,
        projection_sha256="c" * 64,
        structured_head_sha256=head,
        snapshot_sha256="d" * 64,
        generation_sha256="e" * 64,
        receipt_id="f" * 64,
    )


def test_matching_structured_head_wins_over_materialized_baseline(seeded):
    document = seeded["document"]
    proposal = _proposal(
        base="a" * 64,
        head="b" * 64,
        exact="Original sentence",
    )

    result = assess_proposal_applicability(
        proposal,
        document,
        structured_head_sha256="b" * 64,
        current_projection=None,
    )

    assert result.status == "applicable"
    assert result.reason == "same_structured_head"


def test_unrelated_document_change_reanchors_the_original_target(seeded):
    document = seeded["document"]
    proposal = _proposal(
        base="a" * 64,
        head="1" * 64,
        exact="Original sentence",
    )

    result = assess_proposal_applicability(
        proposal,
        document,
        structured_head_sha256="b" * 64,
        current_projection=_projection(
            "A new introduction. Original sentence remains here."
        ),
    )

    assert result.status == "applicable"
    assert result.reason == "reanchored"
    assert result.resolved_start == 20


def test_changed_and_ambiguous_targets_are_typed(seeded):
    document = seeded["document"]
    proposal = _proposal(
        base="a" * 64,
        head="1" * 64,
        exact="Original sentence",
    )

    missing = assess_proposal_applicability(
        proposal,
        document,
        structured_head_sha256="b" * 64,
        current_projection=_projection("The passage was rewritten."),
    )
    ambiguous = assess_proposal_applicability(
        proposal,
        document,
        structured_head_sha256="b" * 64,
        current_projection=_projection(
            "Original sentence. Later, Original sentence."
        ),
    )

    assert (missing.status, missing.reason) == ("target_changed", "target_missing")
    assert (ambiguous.status, ambiguous.reason) == (
        "target_changed",
        "target_ambiguous",
    )


def test_unavailable_projection_is_unknown_not_a_false_stale_claim(seeded):
    document = seeded["document"]
    result = assess_proposal_applicability(
        _proposal(base="a" * 64, head="1" * 64, exact="Original sentence"),
        document,
        structured_head_sha256="b" * 64,
        current_projection=None,
        projection_unavailable_reason="projection_receipt_unavailable",
    )

    assert result.status == "unknown"
    assert result.reason == "projection_receipt_unavailable"
