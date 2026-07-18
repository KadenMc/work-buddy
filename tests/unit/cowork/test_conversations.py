"""Unit tests for co-work document conversations.

All data is labeled throwaway per the live-test data rule. The house
conversations store is exercised against a per-test temporary database threaded
through the ``conn`` parameter, so no test touches the shared conversations DB.
"""

from __future__ import annotations

import sqlite3

import pytest

from work_buddy.conversations.store import _ensure_schema, get_conversation_with_messages
from work_buddy.cowork import conversations as cw
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import new_id, parse_truth_uri


@pytest.fixture
def conv_conn(tmp_path):
    """A throwaway conversations database with the house schema applied."""
    conn = sqlite3.connect(str(tmp_path / "throwaway-conversations.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    yield conn
    conn.close()


def _messages(conn, conversation_id):
    bundle = get_conversation_with_messages(conversation_id, conn=conn)
    assert bundle is not None
    return bundle["messages"]


def test_ensure_document_conversation_lazy_creation_is_idempotent(conv_conn):
    store_id = new_id()
    first = cw.ensure_document_conversation(
        document_id="throwaway-doc-1", store_id=store_id, conn=conv_conn
    )
    assert first.created is True

    second = cw.ensure_document_conversation(
        document_id="throwaway-doc-1", store_id=store_id, conn=conv_conn
    )
    assert second.created is False
    assert second.conversation_id == first.conversation_id


def test_ensure_document_conversation_separates_documents(conv_conn):
    store_id = new_id()
    one = cw.ensure_document_conversation(
        document_id="throwaway-doc-a", store_id=store_id, conn=conv_conn
    )
    two = cw.ensure_document_conversation(
        document_id="throwaway-doc-b", store_id=store_id, conn=conv_conn
    )
    assert one.conversation_id != two.conversation_id


def test_ensure_document_conversation_separates_stores(conv_conn):
    same_document = "throwaway-shared-doc"
    one = cw.ensure_document_conversation(
        document_id=same_document, store_id=new_id(), conn=conv_conn
    )
    two = cw.ensure_document_conversation(
        document_id=same_document, store_id=new_id(), conn=conv_conn
    )
    assert one.conversation_id != two.conversation_id


def test_post_feedback_message_lands_as_user_text(conv_conn):
    binding = cw.ensure_document_conversation(
        document_id="throwaway-doc-2", store_id=new_id(), conn=conv_conn
    )
    text = "Throwaway feedback: this sentence reads as unclear."
    message = cw.post_feedback_message(
        conversation_id=binding.conversation_id, text=text, conn=conv_conn
    )
    assert message is not None
    assert message.role == "user"
    assert message.message_type == "text"
    assert message.content == text

    stored = _messages(conv_conn, binding.conversation_id)
    assert stored[-1]["role"] == "user"
    assert stored[-1]["content"] == text


def test_post_feedback_message_rejects_blank_text(conv_conn):
    binding = cw.ensure_document_conversation(
        document_id="throwaway-doc-3", store_id=new_id(), conn=conv_conn
    )
    with pytest.raises(InvariantViolation):
        cw.post_feedback_message(
            conversation_id=binding.conversation_id, text="   ", conn=conv_conn
        )


def test_feedback_poster_posts_and_returns_references(conv_conn):
    store_id = new_id()
    poster = cw.feedback_poster(
        document_id="throwaway-doc-4", store_id=store_id, conn=conv_conn
    )
    first = poster("Throwaway feedback one.")
    second = poster("Throwaway feedback two.")

    # One conversation per document: both posts land in the same conversation
    # but as distinct messages.
    assert first.conversation_id == second.conversation_id
    assert first.message_id != second.message_id

    stored = _messages(conv_conn, first.conversation_id)
    contents = [message["content"] for message in stored]
    assert "Throwaway feedback one." in contents
    assert "Throwaway feedback two." in contents
    assert all(message["role"] == "user" for message in stored)


def test_deliver_redirect_delivers_note_and_reference(conv_conn):
    store_id = new_id()
    proposal_id = new_id()
    status = cw.deliver_decision(
        document_id="throwaway-doc-5",
        store_id=store_id,
        verb="redirect",
        proposal_id=proposal_id,
        note="Throwaway note: keep the hedge, soften the claim.",
        conn=conv_conn,
    )
    assert status.delivered is True
    assert status.verb == "redirect"
    assert status.proposal_id == proposal_id
    assert status.message_id is not None

    stored = _messages(conv_conn, status.conversation_id)
    delivered = stored[-1]
    assert delivered["role"] == "user"
    assert "Throwaway note: keep the hedge, soften the claim." in delivered["content"]
    reference = parse_truth_uri(
        [token for token in delivered["content"].split() if token.startswith("wb-truth://")][0]
    )
    assert reference.kind == "proposal"
    assert reference.record_id == proposal_id
    assert reference.store_id == store_id


def test_deliver_endorse_delivers_reference_without_note(conv_conn):
    store_id = new_id()
    proposal_id = new_id()
    status = cw.deliver_decision(
        document_id="throwaway-doc-6",
        store_id=store_id,
        verb="endorse",
        proposal_id=proposal_id,
        conn=conv_conn,
    )
    assert status.delivered is True
    assert status.verb == "endorse"

    stored = _messages(conv_conn, status.conversation_id)
    delivered = stored[-1]
    assert "linked proposal" in delivered["content"]
    assert f"wb-truth://{store_id}/proposal/{proposal_id}" in delivered["content"]


def test_deliver_redirect_requires_a_note(conv_conn):
    with pytest.raises(InvariantViolation):
        cw.deliver_decision(
            document_id="throwaway-doc-7",
            store_id=new_id(),
            verb="redirect",
            proposal_id=new_id(),
            note=None,
            conn=conv_conn,
        )


def test_deliver_rejects_non_routing_verb(conv_conn):
    with pytest.raises(InvariantViolation):
        cw.deliver_decision(
            document_id="throwaway-doc-8",
            store_id=new_id(),
            verb="confirm",
            proposal_id=new_id(),
            conn=conv_conn,
        )


def test_deliver_reuses_existing_document_conversation(conv_conn):
    store_id = new_id()
    binding = cw.ensure_document_conversation(
        document_id="throwaway-doc-9", store_id=store_id, conn=conv_conn
    )
    status = cw.deliver_decision(
        document_id="throwaway-doc-9",
        store_id=store_id,
        verb="endorse",
        proposal_id=new_id(),
        conn=conv_conn,
    )
    assert status.delivered is True
    assert status.conversation_id == binding.conversation_id


def test_deliver_reports_undelivered_for_closed_conversation(conv_conn):
    from work_buddy.conversations.store import close_conversation

    store_id = new_id()
    binding = cw.ensure_document_conversation(
        document_id="throwaway-doc-10", store_id=store_id, conn=conv_conn
    )
    close_conversation(binding.conversation_id, conn=conv_conn)

    status = cw.deliver_decision(
        document_id="throwaway-doc-10",
        store_id=store_id,
        verb="endorse",
        proposal_id=new_id(),
        conn=conv_conn,
    )
    assert status.delivered is False
    assert status.reason == "conversation_unavailable"
