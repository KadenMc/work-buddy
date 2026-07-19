"""Document-bound conversations for the Co-work surface.

One conversation exists per registered co-work document, created lazily on the
house conversations store (``work_buddy.conversations.store``). The binding is
not a new table: a conversation tagged in its own ``metadata`` with the truth
store id and the document id IS the mapping, discoverable by a source-scoped
metadata lookup. The truth ``documents`` row cannot carry the binding because it
is append-only and this module does not own the store DDL, so the mapping lives
on the conversation side instead.

Three jobs live here:

* ``ensure_document_conversation`` finds or lazily creates the single
  conversation for a document. It uses ``create_conversation`` directly, never
  the ``conversation_create`` capability, so the surface does not double mount
  (the capability fires a chat toast and a workflow tab, per the chat-sidebar
  consumer pattern).
* ``feedback_poster`` returns a hook that posts the human's verbatim feedback as
  the user's message and reports the conversation and message references. The
  feedback-capture engine (``work_buddy.cowork.feedback``) calls that hook.
* ``deliver_decision`` routes a ``redirect`` note or an ``endorse`` decision into
  the document conversation as an agent-facing message, returning delivery
  status. The background document agent reads that message and acts on it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from work_buddy.conversations.models import ConversationMessage
from work_buddy.conversations.store import (
    add_message,
    create_conversation,
    get_connection,
)
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import truth_uri


# Every co-work document conversation carries this source label and a metadata
# tag naming its document. The pair is the table-free document-to-conversation
# mapping this module owns.
CONVERSATION_SOURCE = "cowork_document"
_DOCUMENT_ID_KEY = "cowork_document_id"
_STORE_ID_KEY = "cowork_store_id"
_KIND_KEY = "cowork_kind"
_KIND_VALUE = "document_conversation"
_DEFAULT_TITLE = "Co-work document conversation"

# The two proposal decisions whose routing this module delivers. Redirect
# carries a typed human note, endorse carries none (PRD section 6).
ROUTING_VERBS = frozenset({"redirect", "endorse"})


@dataclass(frozen=True, slots=True)
class PostedFeedback:
    """The conversation and message a feedback posting landed in."""

    conversation_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class ConversationBinding:
    """The document conversation resolved for a document, and whether it is new."""

    conversation_id: str
    document_id: str
    store_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class DeliveryStatus:
    """The outcome of routing a redirect or endorse into a document conversation."""

    delivered: bool
    conversation_id: str
    verb: str
    proposal_id: str
    message_id: str | None = None
    reason: str | None = None


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvariantViolation(f"{label} must be a nonempty string")
    return value


def _find_bound_conversation(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    store_id: str,
) -> str | None:
    """Return the existing conversation id bound to a document, or None.

    The lookup scans only the co-work source rows and matches the document and
    store tags in metadata. The number of co-work conversations is one per
    document, so the scan stays small.
    """
    rows = conn.execute(
        "SELECT conversation_id, metadata FROM conversations "
        "WHERE source = ? ORDER BY created_at ASC, conversation_id ASC",
        (CONVERSATION_SOURCE,),
    ).fetchall()
    for row in rows:
        raw_meta = row["metadata"]
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        if (
            meta.get(_DOCUMENT_ID_KEY) == document_id
            and meta.get(_STORE_ID_KEY) == store_id
        ):
            return row["conversation_id"]
    return None


def ensure_document_conversation(
    *,
    document_id: str,
    store_id: str,
    title: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> ConversationBinding:
    """Find or lazily create the single conversation for a document.

    Idempotent: a repeat call for the same document and store returns the same
    conversation without creating a second one. When no connection is supplied a
    fresh one is opened and closed here.
    """
    doc_id = _require_text(document_id, "document_id")
    store_ref = _require_text(store_id, "store_id")
    own_conn = conn is None
    active = get_connection() if own_conn else conn
    try:
        existing = _find_bound_conversation(
            active, document_id=doc_id, store_id=store_ref
        )
        if existing is not None:
            return ConversationBinding(
                conversation_id=existing,
                document_id=doc_id,
                store_id=store_ref,
                created=False,
            )
        conversation = create_conversation(
            title=title or _DEFAULT_TITLE,
            source=CONVERSATION_SOURCE,
            metadata={
                _DOCUMENT_ID_KEY: doc_id,
                _STORE_ID_KEY: store_ref,
                _KIND_KEY: _KIND_VALUE,
            },
            conn=active,
        )
        return ConversationBinding(
            conversation_id=conversation.conversation_id,
            document_id=doc_id,
            store_id=store_ref,
            created=True,
        )
    finally:
        if own_conn:
            active.close()


def post_feedback_message(
    *,
    conversation_id: str,
    text: str,
    conn: sqlite3.Connection | None = None,
) -> ConversationMessage | None:
    """Post the human's verbatim feedback as the user's message.

    The feedback is authored content, not an agent utterance, so it lands with
    the user role and the text is stored exactly as written. Returns the stored
    message, or None when the conversation is missing or closed.
    """
    conversation = _require_text(conversation_id, "conversation_id")
    if not isinstance(text, str) or not text.strip():
        raise InvariantViolation("feedback text must be a nonempty string")
    own_conn = conn is None
    active = get_connection() if own_conn else conn
    try:
        return add_message(
            conversation,
            "user",
            text,
            message_type="text",
            conn=active,
        )
    finally:
        if own_conn:
            active.close()


def feedback_poster(
    *,
    document_id: str,
    store_id: str,
    title: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> Callable[[str], PostedFeedback]:
    """Return a hook that posts feedback into a document's conversation.

    The hook resolves (or lazily creates) the document conversation, posts the
    verbatim feedback as the user's message, and returns the conversation and
    message references. The feedback-capture engine passes the returned message
    reference into the evidence locator.
    """
    doc_id = _require_text(document_id, "document_id")
    store_ref = _require_text(store_id, "store_id")

    def _post(text: str) -> PostedFeedback:
        binding = ensure_document_conversation(
            document_id=doc_id,
            store_id=store_ref,
            title=title,
            conn=conn,
        )
        message = post_feedback_message(
            conversation_id=binding.conversation_id,
            text=text,
            conn=conn,
        )
        if message is None:
            raise InvariantViolation(
                "could not post feedback into conversation "
                f"{binding.conversation_id}"
            )
        return PostedFeedback(
            conversation_id=binding.conversation_id,
            message_id=message.message_id,
        )

    return _post


def _proposal_reference(store_id: str, proposal_id: str) -> str:
    """Return a durable proposal reference, a wb-truth URI when the ids allow it."""
    try:
        return truth_uri(store_id, "proposal", proposal_id)
    except (ValueError, TypeError):
        return f"proposal:{proposal_id}"


def _compose_routing_message(verb: str, proposal_reference: str, note: str | None) -> str:
    """Compose the agent-facing message for a routed decision.

    The wording is a functional default pending word-by-word review of surface
    copy. Redirect carries the human's verbatim note, endorse carries the
    contract instruction that a drafted fix returns as a new linked proposal.
    """
    if verb == "redirect":
        return f"Redirect on {proposal_reference}\n\n{note}"
    return (
        f"Endorse on {proposal_reference}\n\n"
        "The flagged problem is confirmed. Draft a fix and return it as a new "
        "linked proposal for review."
    )


def deliver_decision(
    *,
    document_id: str,
    store_id: str,
    verb: str,
    proposal_id: str,
    note: str | None = None,
    title: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> DeliveryStatus:
    """Deliver a redirect note or endorse into the document conversation.

    Resolves (or lazily creates) the document conversation, posts an agent-facing
    message carrying the note and the proposal reference, and reports whether it
    was delivered. Redirect requires a typed note, endorse carries none.
    """
    if verb not in ROUTING_VERBS:
        raise InvariantViolation(
            f"deliverable decision verb must be one of {sorted(ROUTING_VERBS)}"
        )
    proposal = _require_text(proposal_id, "proposal_id")
    if verb == "redirect" and not (isinstance(note, str) and note.strip()):
        raise InvariantViolation("redirect delivery requires a typed note")

    own_conn = conn is None
    active = get_connection() if own_conn else conn
    try:
        binding = ensure_document_conversation(
            document_id=document_id,
            store_id=store_id,
            title=title,
            conn=active,
        )
        content = _compose_routing_message(
            verb, _proposal_reference(binding.store_id, proposal), note
        )
        message = add_message(
            binding.conversation_id,
            "user",
            content,
            message_type="text",
            conn=active,
        )
        if message is None:
            return DeliveryStatus(
                delivered=False,
                conversation_id=binding.conversation_id,
                verb=verb,
                proposal_id=proposal,
                message_id=None,
                reason="conversation_unavailable",
            )
        return DeliveryStatus(
            delivered=True,
            conversation_id=binding.conversation_id,
            verb=verb,
            proposal_id=proposal,
            message_id=message.message_id,
            reason=None,
        )
    finally:
        if own_conn:
            active.close()


__all__ = [
    "CONVERSATION_SOURCE",
    "ROUTING_VERBS",
    "ConversationBinding",
    "DeliveryStatus",
    "PostedFeedback",
    "deliver_decision",
    "ensure_document_conversation",
    "feedback_poster",
    "post_feedback_message",
]
