"""Unit tests for the R5 sitting decision policy (per-item, partial-failure)."""

from __future__ import annotations

import pytest

from work_buddy.cowork import sittings
from work_buddy.truth import proposals
from work_buddy.truth.identity import sha256_bytes, sha256_text

from .conftest import HUMAN, NOW, gesture_count


def _materialize_block(body: str) -> dict:
    return {"rendered_markdown": body, "post_apply_content_sha256": sha256_text(body)}


def test_confirm_applies_and_materializes(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    proposal = make_proposal()
    new_body = "# Throwaway fixture\n\nRevised sentence for co-work tests.\n"
    response, events = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        materialize=_materialize_block(new_body),
        at=NOW,
    )
    assert response["ok"] is True
    result = response["results"][0]
    assert result["result"] == "applied"
    assert result["materialized"] is True
    assert result["gesture_id"]
    assert response["materialize"]["new_file_sha256"] == sha256_text(new_body)
    # The file was written through the engine.
    assert (seeded["root"] / seeded["rel"]).read_text(encoding="utf-8") == new_body
    kinds = [event_type for event_type, _ in events]
    assert "truth.doc_proposal_decided" in kinds
    assert "truth.doc_materialized" in kinds
    assert "truth.doc_proposal_applied" in kinds


def test_n_marks_mint_n_gestures_with_distinct_hashes(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    quotes = [
        ("Original sentence for co-work tests.", "First revision."),
        ("Second target phrase.", "Second revision."),
        ("Third target phrase.", "Third revision."),
    ]
    made = [
        make_proposal(quote=quote, replacement=replacement)
        for quote, replacement in quotes
    ]
    items = [
        {
            "proposal_id": proposal.id,
            "verb": "confirm",
            "canonical_sha256": proposal.canonical_sha256,
        }
        for proposal in made
    ]
    new_body = "materialized body"
    response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=items,
        materialize=_materialize_block(new_body),
        at=NOW,
    )
    assert all(result["result"] == "applied" for result in response["results"])
    with store.connect() as conn:
        rows = conn.execute("SELECT payload_sha256 FROM gestures").fetchall()
    assert len(rows) == 3
    assert len({row["payload_sha256"] for row in rows}) == 3


def test_stale_view_is_rejected_and_mints_no_gesture(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    proposal = make_proposal()
    response, events = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": "0" * 64,
            }
        ],
        at=NOW,
    )
    result = response["results"][0]
    assert result["result"] == "rejected_stale_view"
    assert result["gesture_id"] is None
    assert gesture_count(store) == 0
    assert events == []


def test_partial_failure_commits_valid_items_only(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    good = make_proposal(quote="Original sentence for co-work tests.", replacement="Good.")
    stale = make_proposal(quote="Second target phrase.", replacement="Stale.")
    response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": good.id,
                "verb": "reject_plain",
                "canonical_sha256": good.canonical_sha256,
            },
            {
                "proposal_id": stale.id,
                "verb": "reject_plain",
                "canonical_sha256": "0" * 64,
            },
        ],
        at=NOW,
    )
    assert response["partial"] is True
    results = {result["proposal_id"]: result for result in response["results"]}
    assert results[good.id]["result"] == "closed"
    assert results[stale.id]["result"] == "rejected_stale_view"
    # Only the valid mark reached a decision.
    assert gesture_count(store) == 1
    assert proposals.latest_proposal_status(store, good.id).status == "closed"
    assert proposals.latest_proposal_status(store, stale.id).status == "open"


def test_stale_base_blocks_apply_but_allows_reject(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    stale_base = sha256_bytes(b"a completely different document body")
    proposal = make_proposal(base_content_sha256=stale_base)
    # confirm on a stale-base proposal errors and mints nothing.
    confirm_response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        at=NOW,
    )
    confirm_result = confirm_response["results"][0]
    assert confirm_result["result"] == "error"
    assert confirm_result["base_ok"] is False
    assert confirm_result["error"] == "stale_base"
    assert gesture_count(store) == 0
    # reject_plain on the same stale-base proposal is allowed.
    reject_response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "reject_plain",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        at=NOW,
    )
    assert reject_response["results"][0]["result"] == "closed"
    assert gesture_count(store) == 1


def test_reject_as_false_records_verbatim_negation(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    proposal = make_proposal()
    response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "reject_as_false",
                "canonical_sha256": proposal.canonical_sha256,
                "negation_text": "The revised sentence is not accurate.",
            }
        ],
        at=NOW,
    )
    result = response["results"][0]
    assert result["result"] == "closed"
    assert result["negation_claim_id"]
    claim = store.get_claim(result["negation_claim_id"])
    assert claim.proposition == "The revised sentence is not accurate."


def test_dismiss_and_endorse_apply_only_to_flags(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    flag = make_proposal(
        quote="Second target phrase.", replacement=None, tldr="Raise a concern."
    )
    edit = make_proposal(quote="Third target phrase.", replacement="An edit.")
    # dismiss closes a flag, endorse keeps it open (routed to the agent).
    dismiss_response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": flag.id,
                "verb": "dismiss",
                "canonical_sha256": flag.canonical_sha256,
            }
        ],
        at=NOW,
    )
    assert dismiss_response["results"][0]["result"] == "closed"
    # endorse is only valid on a flag: an edit errors and mints nothing.
    endorse_response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": edit.id,
                "verb": "endorse",
                "canonical_sha256": edit.canonical_sha256,
            }
        ],
        at=NOW,
    )
    assert endorse_response["results"][0]["result"] == "error"


def test_endorse_keeps_flag_open(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    flag = make_proposal(quote="Second target phrase.", replacement=None)
    response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": flag.id,
                "verb": "endorse",
                "canonical_sha256": flag.canonical_sha256,
            }
        ],
        at=NOW,
    )
    result = response["results"][0]
    assert result["result"] == "kept_open_endorsed"
    assert result["new_proposal_id"] is None
    assert proposals.latest_proposal_status(store, flag.id).status == "open"


def test_reject_as_preference_is_reported_blocked(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    proposal = make_proposal()
    response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "reject_as_preference",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        at=NOW,
    )
    result = response["results"][0]
    assert result["result"] == "error"
    assert "preference-claim channel" in result["error"]
    assert gesture_count(store) == 0


def test_reject_as_false_without_negation_or_refs_mints_nothing(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    proposal = make_proposal()
    response, _ = sittings.apply_sitting(
        store,
        document,
        HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "reject_as_false",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        at=NOW,
    )
    result = response["results"][0]
    assert result["result"] == "error"
    assert "nothing to negate" in result["error"]
    assert gesture_count(store) == 0
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"


def test_materialize_hash_mismatch_aborts_before_commit(seeded, make_proposal):
    store = seeded["store"]
    document = seeded["document"]
    proposal = make_proposal()
    with pytest.raises(sittings.MaterializeHashMismatch):
        sittings.apply_sitting(
            store,
            document,
            HUMAN,
            items=[
                {
                    "proposal_id": proposal.id,
                    "verb": "confirm",
                    "canonical_sha256": proposal.canonical_sha256,
                }
            ],
            materialize={
                "rendered_markdown": "some body",
                "post_apply_content_sha256": "0" * 64,
            },
            at=NOW,
        )
    # No decision committed: the proposal is still open and no gesture was minted.
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"
    assert gesture_count(store) == 0
