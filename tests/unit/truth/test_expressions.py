"""Invariant tests for document spans and the expression relation."""

from __future__ import annotations

import pytest

from work_buddy.truth import documents, expressions
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import sha256_text, truth_uri
from work_buddy.truth.lifecycle import TruthLifecycle


NOW = "2026-07-17T12:00:00.000+00:00"
LATER = "2026-07-17T12:05:00.000+00:00"
HUMAN = Actor("human", "reviewer-kaden")
AGENT = Actor(
    "agent_run",
    "cowork-agent-run",
    {
        "model": "test-model",
        "harness": "pytest",
        "surface": "cowork",
        "session_id": "session-1",
        "call_id": "call-1",
    },
)

_SELECTOR = [{"type": "TextQuoteSelector", "exact": "the passage", "prefix": "", "suffix": ""}]


def _claim(store, proposition="The passage asserts a fact"):
    return store.propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim


def test_ensure_document_span_reuses_by_span_sha256(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    first = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    second = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=LATER,
    )
    assert first.id == second.id
    assert first.span_sha256 == sha256_text("the passage")


def test_mark_expression_captures_both_fingerprints(document_store, register_document):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    claim = _claim(store)
    span = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    expression = expressions.mark_expression(
        store,
        document_span_id=span.id,
        claim_ref=claim.id,
        role="summary",
        actor=HUMAN,
        at=NOW,
    )
    assert expression.claim_canonical_sha256 == claim.canonical_sha256
    assert expression.span_sha256 == span.span_sha256
    assert expression.role == "summary"
    assert expression.claim_ref_kind == "local"


def test_mark_expression_rejects_bad_role(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    claim = _claim(store)
    span = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    with pytest.raises(InvariantViolation):
        expressions.mark_expression(
            store,
            document_span_id=span.id,
            claim_ref=claim.id,
            role="opinion",
            actor=HUMAN,
        )


def test_mark_expression_accepts_same_store_uri(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    claim = _claim(store)
    span = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    uri = truth_uri(store.store_id, "claim", claim.id)
    expression = expressions.mark_expression(
        store,
        document_span_id=span.id,
        claim_ref=uri,
        role="quote",
        actor=HUMAN,
    )
    assert expression.claim_ref_kind == "uri"
    assert expression.claim_canonical_sha256 == claim.canonical_sha256


def test_expressions_for_document_and_claim(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    claim = _claim(store)
    span = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    expression = expressions.mark_expression(
        store,
        document_span_id=span.id,
        claim_ref=claim.id,
        role="instantiation",
        actor=HUMAN,
    )
    doc_hits = expressions.expressions_for_document(store, document_id)
    claim_hits = expressions.expressions_for_claim(store, claim.id)
    assert [e.id for e in doc_hits] == [expression.id]
    assert [e.id for e in claim_hits] == [expression.id]


def test_claim_side_staleness_from_redaction(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    claim = _claim(store)
    span = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    expressions.mark_expression(
        store,
        document_span_id=span.id,
        claim_ref=claim.id,
        role="instantiation",
        actor=HUMAN,
        at=NOW,
    )
    assert expressions.stale_expressions(store, document_id=document_id) == ()

    # Rejecting the claim redacts it and moves it to a terminal status, which
    # mechanically stales its expression (claim-side).
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="reject_plain",
        displayed_payload_sha256=claim.canonical_sha256,
        at=NOW,
    )
    lifecycle.reject_claim(
        source_claim_id=claim.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_plain",
        expected_context_sha256=None,
        observed_at=LATER,
        at=LATER,
    )
    stale = expressions.stale_expressions(store, document_id=document_id)
    assert len(stale) == 1
    assert stale[0].claim_side_stale is True


def test_span_side_staleness_from_document_edit(document_store, register_document):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    claim = _claim(store)
    span = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    expressions.mark_expression(
        store,
        document_span_id=span.id,
        claim_ref=claim.id,
        role="instantiation",
        actor=HUMAN,
        at=NOW,
    )
    assert expressions.stale_expressions(store, document_id=document_id) == ()
    # The document content advances (edit drift), staling the span side.
    documents.record_materialization(
        store,
        document_id=document_id,
        content_sha256=sha256_text("edited body"),
        actor=HUMAN,
        at=LATER,
    )
    stale = expressions.stale_expressions(store, document_id=document_id)
    assert len(stale) == 1
    assert stale[0].span_side_stale is True


def test_mark_expression_requires_resolvable_claim(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    span = expressions.ensure_document_span(
        store,
        document_id=document_id,
        selector=_SELECTOR,
        quote_exact="the passage",
        actor=HUMAN,
        at=NOW,
    )
    with pytest.raises(InvariantViolation):
        expressions.mark_expression(
            store,
            document_span_id=span.id,
            claim_ref="f" * 32,
            role="instantiation",
            actor=HUMAN,
        )
