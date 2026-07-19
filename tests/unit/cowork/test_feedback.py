"""Unit tests for span-anchored feedback capture.

All data is labeled throwaway per the live-test data rule. The conversation
posting hook is a double, so these tests exercise only the truth-engine side
(evidence, document span, locator, event) without the conversations database.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from work_buddy.cowork.feedback import FeedbackSpan, capture_feedback
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import new_id, sha256_bytes, sha256_text
from work_buddy.truth.store import TruthStore


NOW = "2026-07-17T12:00:00.000+00:00"
HUMAN = Actor("human", "reviewer-throwaway")
SYSTEM = Actor("system", "system-throwaway")
AGENT = Actor(
    "agent_run",
    "agent-throwaway",
    {
        "model": "throwaway-model",
        "harness": "pytest",
        "surface": "cowork",
        "session_id": "throwaway-session",
    },
)

_BODY = "# Throwaway\n\nOriginal throwaway sentence for feedback tests.\n"
_QUOTE = "Original throwaway sentence for feedback tests."


def _profile(*, enabled=True, feedback_capture=True):
    return {
        "store_id": new_id(),
        "profile": "cowork-doc-test",
        "title": "Throwaway co-work store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard", "cli", "chat_consent"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "document_surface": {
            "enabled": enabled,
            "allowed_document_classes": ["co_authored", "generated"],
            "feedback_capture": feedback_capture,
        },
    }


def _make_store(tmp_path, **profile_kwargs):
    return TruthStore.create(tmp_path / "scope", _profile(**profile_kwargs))


def _register(store):
    record = documents.register_document(
        store,
        path="docs/throwaway-feedback.md",
        title="Throwaway",
        document_class="co_authored",
        content_sha256=sha256_bytes(_BODY.encode("utf-8")),
        actor=HUMAN,
        at=NOW,
    )
    return record.id


class _FakePoster:
    """A conversation posting double that hands back deterministic references."""

    def __init__(self, conversation_id="throwaway-conv"):
        self.conversation_id = conversation_id
        self.calls: list[str] = []

    def __call__(self, text):
        message_id = f"throwaway-msg-{len(self.calls)}"
        self.calls.append(text)
        return SimpleNamespace(
            conversation_id=self.conversation_id, message_id=message_id
        )


class _EmitSpy:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, event_type, *, store_id, subject_kind=None, subject_id=None, data=None):
        self.calls.append(
            {
                "event_type": event_type,
                "store_id": store_id,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "data": data,
            }
        )
        return SimpleNamespace(event_id="throwaway-evt", published=True, error=None)


def _span_row(store, span_id):
    conn = store.connect()
    try:
        return conn.execute(
            "SELECT * FROM document_spans WHERE id = ?", (span_id,)
        ).fetchone()
    finally:
        conn.close()


def _evidence_count(store):
    conn = store.connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
    finally:
        conn.close()


def test_feedback_is_captured_as_user_authored_utterance(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)
    poster = _FakePoster()
    emit = _EmitSpy()

    result = capture_feedback(
        store,
        document_id=document_id,
        span=FeedbackSpan(exact=_QUOTE),
        verbatim_text="This sentence is unclear to me.",
        actor=HUMAN,
        post_message=poster,
        at=NOW,
        emit_event=emit,
    )

    assert result.trust_class == "user_authored"
    assert result.locator == f"wb-session://{poster.conversation_id}/{result.message_id}"
    assert poster.calls == ["This sentence is unclear to me."]

    evidence = store.get_evidence(result.evidence_id)
    assert evidence is not None
    assert evidence.kind == "utterance"
    assert evidence.trust_class == "user_authored"
    assert evidence.content == "This sentence is unclear to me."
    assert evidence.acquisition_method == "said_in_chat"
    assert evidence.acquired_by_kind == "human"
    assert evidence.content_sha256 == sha256_text("This sentence is unclear to me.")


def test_feedback_emits_the_capture_event(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)
    emit = _EmitSpy()

    result = capture_feedback(
        store,
        document_id=document_id,
        span=FeedbackSpan(exact=_QUOTE),
        verbatim_text="Please cite a source here.",
        actor=HUMAN,
        post_message=_FakePoster(),
        at=NOW,
        emit_event=emit,
    )

    assert result.event_published is True
    assert len(emit.calls) == 1
    call = emit.calls[0]
    assert call["event_type"] == "truth.doc_feedback_captured"
    assert call["store_id"] == store.store_id
    assert call["data"] == {
        "document_id": document_id,
        "evidence_id": result.evidence_id,
        "conversation_id": result.conversation_id,
    }


def test_document_span_is_anchored_and_human_authored(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)

    result = capture_feedback(
        store,
        document_id=document_id,
        span=FeedbackSpan(exact=_QUOTE),
        verbatim_text="Anchor check.",
        actor=HUMAN,
        post_message=_FakePoster(),
        at=NOW,
        emit_event=_EmitSpy(),
    )

    row = _span_row(store, result.document_span_id)
    assert row is not None
    assert row["document_id"] == document_id
    assert row["quote_exact"] == _QUOTE
    assert row["span_sha256"] == sha256_text(_QUOTE)
    assert row["author_kind"] == "human"


def test_repeated_feedback_reuses_the_document_span(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)
    poster = _FakePoster()

    first = capture_feedback(
        store,
        document_id=document_id,
        span=FeedbackSpan(exact=_QUOTE),
        verbatim_text="First remark on the same passage.",
        actor=HUMAN,
        post_message=poster,
        at=NOW,
        emit_event=_EmitSpy(),
    )
    second = capture_feedback(
        store,
        document_id=document_id,
        span=FeedbackSpan(exact=_QUOTE),
        verbatim_text="Second remark on the same passage.",
        actor=HUMAN,
        post_message=poster,
        at=NOW,
        emit_event=_EmitSpy(),
    )

    # The anchor dedups by span hash, but each remark is its own evidence.
    assert first.document_span_id == second.document_span_id
    assert first.evidence_id != second.evidence_id
    assert _evidence_count(store) == 2


def test_evidence_meta_records_the_locator_gap_fields(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)

    result = capture_feedback(
        store,
        document_id=document_id,
        span=FeedbackSpan(exact=_QUOTE, node_id_hint="node-throwaway-1"),
        verbatim_text="Gap-field check.",
        actor=HUMAN,
        post_message=_FakePoster(),
        at=NOW,
        emit_event=_EmitSpy(),
    )

    evidence = store.get_evidence(result.evidence_id)
    meta = json.loads(evidence.meta_json)
    binding = meta["cowork_feedback"]
    assert binding["document_id"] == document_id
    assert binding["document_span_id"] == result.document_span_id
    assert binding["conversation_id"] == result.conversation_id
    assert binding["message_id"] == result.message_id
    assert binding["node_id_hint"] == "node-throwaway-1"
    assert meta["locator"]["scheme"] == "wb-session"


def test_agent_actor_cannot_author_feedback(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)
    poster = _FakePoster()

    with pytest.raises(InvariantViolation):
        capture_feedback(
            store,
            document_id=document_id,
            span=FeedbackSpan(exact=_QUOTE),
            verbatim_text="An agent must not be able to author this.",
            actor=AGENT,
            post_message=poster,
            at=NOW,
            emit_event=_EmitSpy(),
        )

    # Fail fast: no message posted and no evidence written.
    assert poster.calls == []
    assert _evidence_count(store) == 0


def test_system_actor_cannot_author_feedback(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)
    poster = _FakePoster()

    with pytest.raises(InvariantViolation):
        capture_feedback(
            store,
            document_id=document_id,
            span=FeedbackSpan(exact=_QUOTE),
            verbatim_text="A system actor must not be able to author this.",
            actor=SYSTEM,
            post_message=poster,
            at=NOW,
            emit_event=_EmitSpy(),
        )
    assert poster.calls == []
    assert _evidence_count(store) == 0


def test_blank_feedback_text_is_rejected_before_posting(tmp_path):
    store = _make_store(tmp_path)
    document_id = _register(store)
    poster = _FakePoster()

    with pytest.raises(InvariantViolation):
        capture_feedback(
            store,
            document_id=document_id,
            span=FeedbackSpan(exact=_QUOTE),
            verbatim_text="   ",
            actor=HUMAN,
            post_message=poster,
            at=NOW,
            emit_event=_EmitSpy(),
        )
    assert poster.calls == []
    assert _evidence_count(store) == 0


def test_feedback_on_unknown_document_is_rejected(tmp_path):
    store = _make_store(tmp_path)
    _register(store)
    poster = _FakePoster()

    with pytest.raises(InvariantViolation):
        capture_feedback(
            store,
            document_id=new_id(),
            span=FeedbackSpan(exact=_QUOTE),
            verbatim_text="Feedback on a document that was never registered.",
            actor=HUMAN,
            post_message=poster,
            at=NOW,
            emit_event=_EmitSpy(),
        )
    assert poster.calls == []


def test_feedback_capture_disabled_by_profile_is_rejected(tmp_path):
    store = _make_store(tmp_path, feedback_capture=False)
    document_id = _register(store)
    poster = _FakePoster()

    with pytest.raises(InvariantViolation):
        capture_feedback(
            store,
            document_id=document_id,
            span=FeedbackSpan(exact=_QUOTE),
            verbatim_text="Feedback while the profile disables capture.",
            actor=HUMAN,
            post_message=poster,
            at=NOW,
            emit_event=_EmitSpy(),
        )
    assert poster.calls == []


def test_feedback_rejected_when_document_surface_disabled(tmp_path):
    store = _make_store(tmp_path, enabled=False, feedback_capture=False)
    document_id = _register(store)
    poster = _FakePoster()

    with pytest.raises(InvariantViolation):
        capture_feedback(
            store,
            document_id=document_id,
            span=FeedbackSpan(exact=_QUOTE),
            verbatim_text="Feedback while the surface is disabled.",
            actor=HUMAN,
            post_message=poster,
            at=NOW,
            emit_event=_EmitSpy(),
        )
    assert poster.calls == []
