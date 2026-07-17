"""Co-work identity extensions: the four new truth-record kinds (WP-A2)."""

from __future__ import annotations

import pytest

from work_buddy.truth.identity import (
    TRUTH_RECORD_KINDS,
    new_id,
    parse_truth_uri,
    truth_uri,
)


COWORK_KINDS = ("document", "document_span", "expression", "proposal")


def test_record_kinds_grow_the_four_cowork_kinds() -> None:
    assert {"claim", "evidence", "span", "derivation"} <= TRUTH_RECORD_KINDS
    for kind in COWORK_KINDS:
        assert kind in TRUTH_RECORD_KINDS


def test_audit_row_kinds_are_not_cross_store_referenceable() -> None:
    # proposal_status_event and doc_event are internal audit rows, never minted
    # as cross-store references.
    assert "proposal_status_event" not in TRUTH_RECORD_KINDS
    assert "doc_event" not in TRUTH_RECORD_KINDS


@pytest.mark.parametrize("kind", COWORK_KINDS)
def test_truth_uri_round_trips_for_cowork_kinds(kind: str) -> None:
    store_id = new_id()
    record_id = new_id()
    uri = truth_uri(store_id, kind, record_id)
    assert uri == f"wb-truth://{store_id}/{kind}/{record_id}"
    parsed = parse_truth_uri(uri)
    assert parsed.store_id == store_id
    assert parsed.kind == kind
    assert parsed.record_id == record_id


def test_audit_kinds_are_rejected_by_uri_builder() -> None:
    with pytest.raises(ValueError):
        truth_uri(new_id(), "proposal_status_event", new_id())
    with pytest.raises(ValueError):
        truth_uri(new_id(), "doc_event", new_id())
