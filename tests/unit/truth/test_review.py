from __future__ import annotations

from dataclasses import replace

import pytest

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import canonical_json
from work_buddy.truth.lifecycle import hash_context
from work_buddy.truth.migrations import REDACTED_SELECTOR_JSON
from work_buddy.truth.review import compose_claim_review
from work_buddy.truth.store import TruthStore


HUMAN = Actor("human", "user-1")
SYSTEM = Actor("system", "truth-test")
AGENT = Actor(
    "agent_run",
    "run-1",
    {
        "model": "test-model",
        "harness": "test-harness",
        "surface": "test",
        "session_id": "run-1",
    },
)


def _profile(store_id: str) -> dict:
    return {
        "store_id": store_id,
        "profile": "review-test",
        "title": "Review Test",
        "allowed_claim_kinds": ["fact"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["chat_consent", "cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }


def test_review_payload_is_bound_to_claim_receipts_and_decision(tmp_path) -> None:
    store = TruthStore.create(tmp_path, _profile("a" * 32))
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///review.txt",
        actor=HUMAN,
        acquisition_method="file_read",
        content="The reviewed value is forty two.",
        origin="human_curated",
        derived_from_store="c" * 32,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="The reviewed value is forty two."),
        actor=HUMAN,
    )
    claim = store.propose_claim(
        proposition="The reviewed value is forty two.",
        claim_kind="fact",
        actor=HUMAN,
    ).claim
    store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
    )

    first = compose_claim_review(store, claim.id, action="confirm")
    again = compose_claim_review(store, claim.id, action="confirm")
    rejected = compose_claim_review(
        store,
        claim.id,
        action="reject",
        decision={"reason_class": "reject_as_false"},
    )

    assert first == again
    assert first.payload_sha256 == claim.canonical_sha256
    assert first.receipts[0].span_id == span.id
    assert first.receipts[0].author_kind == "unknown"
    assert first.receipts[0].derived_from_store == "c" * 32
    assert "derived from store: " + "c" * 32 in first.body
    assert claim.proposition in first.body
    assert rejected.context_sha256 == first.context_sha256
    assert rejected.request_fingerprint != first.request_fingerprint


def test_review_fingerprint_changes_when_active_receipts_change(tmp_path) -> None:
    store = TruthStore.create(tmp_path, _profile("b" * 32))
    claim = store.propose_claim(
        proposition="A claim without a receipt yet.",
        claim_kind="fact",
        actor=HUMAN,
    ).claim
    before = compose_claim_review(store, claim.id, action="confirm")

    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///later.txt",
        actor=HUMAN,
        acquisition_method="file_read",
        content="A claim without a receipt yet.",
        origin="human_curated",
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="A claim without a receipt yet."),
        actor=HUMAN,
    )
    store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
    )
    after = compose_claim_review(store, claim.id, action="confirm")

    assert before.context_sha256 != after.context_sha256
    assert before.request_fingerprint != after.request_fingerprint


def test_review_renders_and_fingerprints_complete_canonical_claim(tmp_path) -> None:
    store = TruthStore.create(tmp_path, _profile("d" * 32))
    claim = store.propose_claim(
        proposition="The measured value is forty two kilograms.",
        claim_kind="fact",
        structured={"subject": "sample", "value": 42, "unit": "kg"},
        scope="project:measurement",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-12-31T23:59:59+00:00",
        actor=HUMAN,
    ).claim

    review = compose_claim_review(store, claim.id, action="confirm")

    expected_payload = {
        "proposition": "The measured value is forty two kilograms.",
        "claim_kind": "fact",
        "structured_json": {
            "subject": "sample",
            "unit": "kg",
            "value": 42,
        },
        "scope": "project:measurement",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": "2026-12-31T23:59:59+00:00",
    }
    assert review.claim_payload == expected_payload
    assert canonical_json(expected_payload) in review.body
    assert review.request_fingerprint == hash_context(
        {
            "action": "confirm",
            "claim_id": claim.id,
            "claim_payload": expected_payload,
            "payload_sha256": claim.canonical_sha256,
            "context_sha256": review.context_sha256,
            "decision": {},
        }
    )


def test_review_refuses_claim_payload_hash_drift(
    tmp_path,
    monkeypatch,
) -> None:
    store = TruthStore.create(tmp_path, _profile("e" * 32))
    claim = store.propose_claim(
        proposition="A claim whose stored hash must still match.",
        claim_kind="fact",
        actor=HUMAN,
    ).claim
    drifted = replace(claim, canonical_sha256="0" * 64)
    monkeypatch.setattr(store, "get_claim", lambda claim_id: drifted)

    with pytest.raises(InvariantViolation, match="canonical payload hash mismatch"):
        compose_claim_review(store, claim.id, action="confirm")


def test_review_flags_agent_only_support_and_span_authorship(tmp_path) -> None:
    store = TruthStore.create(tmp_path, _profile("f" * 32))
    evidence = store.capture_evidence(
        kind="chat",
        source_locator="wb-chat://review/session/message",
        actor=SYSTEM,
        acquisition_method="said_in_chat",
        content="The agent asserted the reviewed value.",
        origin="mixed_transcript",
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="agent asserted the reviewed value"),
        actor=AGENT,
        author_kind="agent_run",
        author_ref="run-1",
    )
    claim = store.propose_claim(
        proposition="The reviewed value was asserted.",
        claim_kind="fact",
        actor=HUMAN,
    ).claim
    store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
    )

    review = compose_claim_review(store, claim.id, action="confirm")

    assert review.agent_authored_only is True
    assert review.receipts[0].author_kind == "agent_run"
    assert review.receipts[0].author_ref == "run-1"
    assert "WARNING: This claim is supported only by agent-authored evidence." in (
        review.body
    )
    assert "author: agent_run:run-1" in review.body


@pytest.mark.parametrize("redacted_subject", ["span", "evidence"])
def test_review_exposes_and_excludes_redacted_support(
    tmp_path,
    redacted_subject: str,
) -> None:
    store = TruthStore.create(tmp_path, _profile("1" * 32))
    evidence = store.capture_evidence(
        kind="chat",
        source_locator="wb-chat://review/redacted/message",
        actor=SYSTEM,
        acquisition_method="said_in_chat",
        content="The agent supplied support that was later redacted.",
        origin="mixed_transcript",
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="agent supplied support"),
        actor=AGENT,
        author_kind="agent_run",
        author_ref="run-1",
    )
    claim = store.propose_claim(
        proposition="The redacted support must not count as usable.",
        claim_kind="fact",
        actor=HUMAN,
    ).claim
    store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
    )
    redacted_at = "2026-07-16T12:00:00+00:00"
    with store.write_transaction() as conn:
        if redacted_subject == "span":
            conn.execute(
                "UPDATE evidence_spans SET quote_exact = NULL, "
                "selector_json = ?, redacted_at = ? WHERE id = ?",
                (REDACTED_SELECTOR_JSON, redacted_at, span.id),
            )
        else:
            conn.execute(
                "UPDATE evidence SET content = NULL, content_path = NULL, "
                "redacted_at = ? WHERE id = ?",
                (redacted_at, evidence.id),
            )

    review = compose_claim_review(store, claim.id, action="confirm")
    receipt = review.receipts[0]

    assert review.agent_authored_only is False
    assert receipt.span_redacted_at == (
        redacted_at if redacted_subject == "span" else None
    )
    assert receipt.evidence_redacted_at == (
        redacted_at if redacted_subject == "evidence" else None
    )
    assert f"{redacted_subject} redacted at: {redacted_at}" in review.body
