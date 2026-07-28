"""Span-anchored feedback capture for the Co-work surface.

Highlight a passage, write freeform feedback, and the words are saved VERBATIM
as kernel evidence (PRD section 5). Feedback is human-authored content, not a
gesture (PRD section 6), so it lands as citable evidence a later claim or
preference proposal can cite as its supporting span.

One ``capture_feedback`` call produces four artifacts:

* an ``evidence`` row of kind ``utterance`` whose trust class the engine assigns
  as ``user_authored`` through the human-surface path (the caller supplies a
  human actor and the USER_INPUT origin, and the engine refuses any non-human
  surface). This module never sets the trust class itself.
* a ``document_span`` anchor row locating the highlighted passage in the
  document, authored by the human.
* the document lifecycle event ``truth.doc_feedback_captured`` on the house
  event spine.
* a first-party source locator on the ``wb-session`` scheme. That registered
  scheme names a session and a message reference, so the conversation is the
  session and the posted message is the message reference. The document id and
  document-span id, which the scheme cannot express in the URI, are recorded in
  the evidence meta (see the locator gap note in the module report).

The conversation posting is injected as a hook so this engine module does not
depend on the conversation store. ``work_buddy.cowork.conversations`` provides
the real hook, tests inject a double.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from work_buddy.cowork.lifecycle_lock import document_lifecycle_lock
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.truth.anchors import CompositeSelector, parse_selector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.documents import current_lifecycle, get_document
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.expressions import ensure_document_span
from work_buddy.truth.identity import sha256_text
from work_buddy.truth.locators import DEFAULT_LOCATOR_REGISTRY, LocatorRegistry
from work_buddy.truth.store import AcquisitionOrigin, TruthStore


FEEDBACK_EVIDENCE_KIND = "utterance"
FEEDBACK_ACQUISITION_METHOD = "said_in_chat"
FEEDBACK_EVENT = "truth.doc_feedback_captured"
FEEDBACK_LOCATOR_SCHEME = "wb-session"


class FeedbackPosting(Protocol):
    """Structural result of the conversation posting hook."""

    conversation_id: str
    message_id: str


FeedbackPoster = Callable[[str], FeedbackPosting]
EventEmitter = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class FeedbackSpan:
    """The highlighted passage a piece of feedback points at (R9 span shape)."""

    exact: str
    prefix: str = ""
    suffix: str = ""
    node_id_hint: str | None = None


@dataclass(frozen=True, slots=True)
class FeedbackCapture:
    """The references produced by one feedback capture."""

    evidence_id: str
    document_span_id: str
    conversation_id: str
    message_id: str
    locator: str
    trust_class: str
    event_published: bool


def _feedback_items_locked(
    store: TruthStore,
    *,
    document_id: str,
    conversation_id: str,
    conn: Any,
) -> list[dict[str, Any]]:
    """Project durable feedback evidence for one exact document conversation."""
    items: list[dict[str, Any]] = []
    rows = conn.execute(
        """SELECT id, content, meta_json, created_at
           FROM evidence
           WHERE kind = ? AND acquisition_method = ?
             AND redacted_at IS NULL
           ORDER BY created_at ASC, id ASC""",
        (FEEDBACK_EVIDENCE_KIND, FEEDBACK_ACQUISITION_METHOD),
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        raw_feedback = meta.get("cowork_feedback") if isinstance(meta, Mapping) else None
        if not isinstance(raw_feedback, Mapping):
            continue
        if (
            raw_feedback.get("document_id") != document_id
            or raw_feedback.get("conversation_id") != conversation_id
        ):
            continue
        message_id = raw_feedback.get("message_id")
        span_id = raw_feedback.get("document_span_id")
        if not (
            isinstance(message_id, str)
            and message_id
            and isinstance(span_id, str)
            and span_id
            and isinstance(row["content"], str)
        ):
            continue
        span = store._get_document_span_locked(conn, span_id)
        if span is None or span.document_id != document_id:
            continue
        try:
            selector = parse_selector(span.selector_json)
        except Exception:
            continue
        node_id_hint = raw_feedback.get("node_id_hint")
        items.append(
            {
                "evidence_id": str(row["id"]),
                "span_id": span_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "text": str(row["content"]),
                "anchor": {
                    "exact": selector.exact,
                    "prefix": selector.prefix,
                    "suffix": selector.suffix,
                    "node_id_hint": (
                        node_id_hint if isinstance(node_id_hint, str) else None
                    ),
                },
            }
        )
    return items


def feedback_items(
    store: TruthStore,
    *,
    document_id: str,
    conversation_id: str | None,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """Return unredacted feedback resolvable by its exact persisted message id.

    The returned array is ordered by evidence creation time and id. Every item
    carries its message id so callers can build an exact lookup without
    conflating repeated feedback text attached to different selections.
    """
    if conversation_id is None:
        return []
    if conn is not None:
        return _feedback_items_locked(
            store,
            document_id=document_id,
            conversation_id=conversation_id,
            conn=conn,
        )
    with store._read_connection() as read_conn:
        return _feedback_items_locked(
            store,
            document_id=document_id,
            conversation_id=conversation_id,
            conn=read_conn,
        )


def _coerce_span(span: FeedbackSpan | Mapping[str, Any]) -> FeedbackSpan:
    if isinstance(span, FeedbackSpan):
        return span
    if isinstance(span, Mapping):
        return FeedbackSpan(
            exact=span.get("exact", ""),
            prefix=span.get("prefix", "") or "",
            suffix=span.get("suffix", "") or "",
            node_id_hint=span.get("node_id_hint"),
        )
    raise InvariantViolation("span must be a FeedbackSpan or a mapping")


def _posting_references(posting: FeedbackPosting) -> tuple[str, str]:
    conversation_id = getattr(posting, "conversation_id", None)
    message_id = getattr(posting, "message_id", None)
    if not (isinstance(conversation_id, str) and conversation_id.strip()):
        raise InvariantViolation(
            "conversation posting hook must return a conversation_id"
        )
    if not (isinstance(message_id, str) and message_id.strip()):
        raise InvariantViolation("conversation posting hook must return a message_id")
    return conversation_id, message_id


def capture_feedback(
    store: TruthStore,
    *,
    document_id: str,
    span: FeedbackSpan | Mapping[str, Any],
    verbatim_text: str,
    actor: Actor,
    post_message: FeedbackPoster,
    at: str | None = None,
    registry: LocatorRegistry = DEFAULT_LOCATOR_REGISTRY,
    emit_event: EventEmitter = emit_truth_event,
) -> FeedbackCapture:
    """Capture span-anchored human feedback as citable kernel evidence.

    Order of effects: validate everything validatable first (fail fast, no
    side effects), anchor the document span, post the feedback as the user's
    message through the hook, then capture the utterance evidence and emit the
    lifecycle event. The trust class is assigned by the engine through the
    human-surface path, never set here.
    """
    with document_lifecycle_lock(store.store_id, document_id):
        return _capture_feedback_locked(
            store,
            document_id=document_id,
            span=span,
            verbatim_text=verbatim_text,
            actor=actor,
            post_message=post_message,
            at=at,
            registry=registry,
            emit_event=emit_event,
        )


def _capture_feedback_locked(
    store: TruthStore,
    *,
    document_id: str,
    span: FeedbackSpan | Mapping[str, Any],
    verbatim_text: str,
    actor: Actor,
    post_message: FeedbackPoster,
    at: str | None,
    registry: LocatorRegistry,
    emit_event: EventEmitter,
) -> FeedbackCapture:
    """Implement feedback capture while the document lifecycle lock is held."""

    if not isinstance(verbatim_text, str) or not verbatim_text.strip():
        raise InvariantViolation("feedback text must be a nonempty string")
    if not isinstance(actor, Actor):
        raise InvariantViolation("actor must be an Actor")
    # Feedback is human-authored content (PRD section 6). Guard before any side
    # effect so a non-human caller neither posts a message nor writes a span.
    # The USER_INPUT origin below makes the engine the ultimate authority: an
    # agent or system actor that reached capture_evidence would be refused by
    # the human-surface trust law.
    if actor.kind != "human":
        raise InvariantViolation(
            "feedback is human-authored content and requires a human surface actor"
        )

    document = get_document(store, document_id)
    surface = store.profile.document_surface
    if current_lifecycle(store, document.id) != "active":
        raise InvariantViolation("feedback cannot be added to a retired document")
    if not document_surface_allowed(store, document):
        raise InvariantViolation(
            "This document is not available in Co-work for this folder"
        )
    if not surface.feedback_capture:
        raise InvariantViolation("feedback is disabled for this folder")

    feedback_span = _coerce_span(span)
    selector = CompositeSelector(
        exact=feedback_span.exact,
        prefix=feedback_span.prefix,
        suffix=feedback_span.suffix,
    )

    span_record = ensure_document_span(
        store,
        document_id=document.id,
        selector=selector,
        quote_exact=feedback_span.exact,
        actor=actor,
        at=at,
    )

    posting = post_message(verbatim_text)
    conversation_id, message_id = _posting_references(posting)

    content_sha256 = sha256_text(verbatim_text)
    raw_locator = f"{FEEDBACK_LOCATOR_SCHEME}://{conversation_id}/{message_id}"
    validation = registry.validate(
        FEEDBACK_EVIDENCE_KIND,
        raw_locator,
        {},
        content_sha256=content_sha256,
    )

    evidence_meta = {
        "cowork_feedback": {
            "document_id": document.id,
            "document_span_id": span_record.id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "node_id_hint": feedback_span.node_id_hint,
            "span_sha256": span_record.span_sha256,
        },
        "locator": {
            "scheme": validation.locator_scheme,
            "verifiability_class": validation.verifiability_class,
            "integrity_recipe": dict(validation.integrity_recipe),
        },
    }

    evidence = store.capture_evidence(
        kind=FEEDBACK_EVIDENCE_KIND,
        source_locator=validation.locator,
        actor=actor,
        acquisition_method=FEEDBACK_ACQUISITION_METHOD,
        content=verbatim_text,
        content_sha256=content_sha256,
        origin=AcquisitionOrigin.USER_INPUT,
        meta=evidence_meta,
        acquired_at=at,
        created_at=at,
    )
    if evidence.trust_class != "user_authored":  # pragma: no cover - engine law
        raise InvariantViolation(
            "feedback evidence must be engine-assigned user_authored trust"
        )

    emission = emit_event(
        FEEDBACK_EVENT,
        store_id=store.store_id,
        subject_kind="document",
        subject_id=document.id,
        data={
            "document_id": document.id,
            "evidence_id": evidence.id,
            "conversation_id": conversation_id,
        },
    )

    return FeedbackCapture(
        evidence_id=evidence.id,
        document_span_id=span_record.id,
        conversation_id=conversation_id,
        message_id=message_id,
        locator=validation.locator,
        trust_class=evidence.trust_class,
        event_published=bool(getattr(emission, "published", False)),
    )


__all__ = [
    "FEEDBACK_ACQUISITION_METHOD",
    "FEEDBACK_EVENT",
    "FEEDBACK_EVIDENCE_KIND",
    "FEEDBACK_LOCATOR_SCHEME",
    "FeedbackCapture",
    "FeedbackPoster",
    "FeedbackPosting",
    "FeedbackSpan",
    "capture_feedback",
    "feedback_items",
]
