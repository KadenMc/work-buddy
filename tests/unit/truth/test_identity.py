from __future__ import annotations

import json

import pytest

from work_buddy.truth.identity import (
    canonical_claim_payload,
    claim_sha256,
    new_id,
    parse_truth_uri,
    truth_uri,
)


def test_new_id_is_uuid4_hex_shape() -> None:
    value = new_id()
    assert len(value) == 32
    assert value == value.lower()
    int(value, 16)


def test_claim_hash_normalizes_semantic_whitespace_and_json_order() -> None:
    first = claim_sha256(
        proposition="  Alex   led the project. ",
        claim_kind="fact",
        structured={"b": " two  words ", "a": 1},
        scope="store",
    )
    second = claim_sha256(
        proposition="Alex led the project.",
        claim_kind="fact",
        structured=json.dumps({"a": 1, "b": "two words"}),
        scope="store",
    )
    assert first == second


def test_canonical_claim_payload_rejects_non_object_structured_json() -> None:
    with pytest.raises(ValueError, match="object"):
        canonical_claim_payload(
            proposition="A fact",
            claim_kind="fact",
            structured="[]",
        )


def test_truth_uri_round_trip() -> None:
    store_id = new_id()
    record_id = new_id()
    value = truth_uri(store_id, "claim", record_id)
    parsed = parse_truth_uri(value)
    assert parsed.store_id == store_id
    assert parsed.kind == "claim"
    assert parsed.record_id == record_id
    assert parsed.uri == value


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/claim/x",
        "wb-truth://bad/claim/bad",
        "wb-truth://00000000000000000000000000000000/unknown/"
        "00000000000000000000000000000000",
    ],
)
def test_truth_uri_rejects_invalid_references(value: str) -> None:
    with pytest.raises(ValueError):
        parse_truth_uri(value)
