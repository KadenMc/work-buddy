"""The R5 sitting path: per-item human decisions on cowork proposals.

A sitting is a batch of marks the human made in one review pass. Each mark
mints exactly one gesture bound to the proposal's canonical_sha256 and drives
one Truth-engine decision, so N marks mint N gestures with N distinct hashes.
Items validate and commit INDEPENDENTLY, so one stale mark never aborts the
sitting (per-item plus partial-failure semantics, S4).

This module calls the engine library directly and holds no Flask, so the route
layer stays a thin adapter and the decision policy is unit-testable on its own.
The route mints no gestures itself, it hands the whole thing here inside its
user_initiated boundary and threads a real dashboard-user actor through.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from work_buddy.truth import documents, proposals
from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.truth.contracts import (
    Actor,
    GestureError,
    InvariantViolation,
    TransitionError,
)
from work_buddy.truth.identity import sha256_text
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.store import DocumentRecord, TruthStore

logger = logging.getLogger(__name__)

# A routing hook is called with (verb, proposal_id, note) for a decision that
# keeps the proposal open and must reach the proposing agent (redirect, endorse).
RoutingDeliver = Callable[[str, str, str | None], object]

# The results whose decision routes into the document conversation for the agent.
_ROUTED_RESULTS = frozenset({"kept_open_redirected", "kept_open_endorsed"})

# The dashboard surface gestures are minted on. accept_proposal requires this
# surface to sit in the store profile's confirmation_surfaces.
DECISION_SURFACE = "dashboard"

# Wire verb (a shipped gesture-kind name, S1) -> the gesture kind minted for it.
# Dismiss is the UI verb that consumes a reject_plain gesture on a flag, so it
# maps onto the reject_plain kind (dismiss is not itself a gesture kind).
_VERB_GESTURE_KIND = {
    "confirm": "confirm",
    "edit_confirm": "edit_confirm",
    "reject_plain": "reject_plain",
    "reject_as_false": "reject_as_false",
    "reject_as_preference": "reject_as_preference",
    "redirect": "redirect",
    "defer": "defer",
    "endorse": "endorse",
    "dismiss": "reject_plain",
}

# Wire verb -> the one per-item result it maps to (S4).
_RESULT_BY_VERB = {
    "confirm": "applied",
    "edit_confirm": "applied",
    "reject_plain": "closed",
    "dismiss": "closed",
    "reject_as_false": "closed",
    "reject_as_preference": "closed",
    "redirect": "kept_open_redirected",
    "defer": "kept_open_deferred",
    "endorse": "kept_open_endorsed",
}

# Verbs that apply an accepted edit to the file and materialize this sitting.
_APPLY_VERBS = frozenset({"confirm", "edit_confirm"})
# Verbs that require a fresh base (the AOV stale gate, S6): apply and the
# forward-routing verbs. Reject verbs and defer stay decidable on a stale base.
_BASE_REQUIRED_VERBS = frozenset({"confirm", "edit_confirm", "redirect", "endorse"})
_ALL_VERBS = frozenset(_VERB_GESTURE_KIND)

_DECISION_ERRORS = (TransitionError, GestureError, InvariantViolation)


class MaterializeHashMismatch(Exception):
    """The posted rendered markdown does not re-hash to its declared digest."""


@dataclass(slots=True)
class ItemOutcome:
    """One decided mark: its R5 results[] entry plus post-commit events."""

    result: dict[str, Any]
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    applied: bool = False


def _base_result(proposal_id: str, verb: str, base_ok: bool) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "verb": verb,
        "result": "error",
        "base_ok": base_ok,
        "gesture_id": None,
        "negation_claim_id": None,
        "preference_claim_id": None,
        "new_proposal_id": None,
        "materialized": False,
        "error": None,
    }


def _error(proposal_id: str, verb: str, base_ok: bool, message: str) -> ItemOutcome:
    entry = _base_result(proposal_id, verb, base_ok)
    entry["result"] = "error"
    entry["error"] = message
    return ItemOutcome(result=entry)


def _stale_view(proposal_id: str, verb: str, base_ok: bool) -> ItemOutcome:
    entry = _base_result(proposal_id, verb, base_ok)
    entry["result"] = "rejected_stale_view"
    entry["error"] = "canonical_sha256 no longer matches the shown proposal"
    return ItemOutcome(result=entry)


def _dispatch(
    store: TruthStore,
    proposal: Any,
    actor: Actor,
    verb: str,
    item: dict[str, Any],
    gesture_id: str,
    at: str | None,
) -> Any:
    proposal_id = proposal.id
    if verb == "confirm":
        return proposals.accept_proposal(
            store, proposal_id=proposal_id, gesture_id=gesture_id, actor=actor, at=at
        )
    if verb == "edit_confirm":
        return proposals.accept_proposal(
            store,
            proposal_id=proposal_id,
            gesture_id=gesture_id,
            actor=actor,
            amended_replacement=item.get("amend_content"),
            at=at,
        )
    if verb == "reject_plain":
        return proposals.reject_proposal(
            store,
            proposal_id=proposal_id,
            gesture_id=gesture_id,
            actor=actor,
            reason_class="reject_plain",
            at=at,
        )
    if verb == "reject_as_false":
        return proposals.decide_reject_as_false(
            store,
            proposal_id=proposal_id,
            gesture_id=gesture_id,
            actor=actor,
            negation_text=item.get("negation_text"),
            at=at,
        )
    if verb == "reject_as_preference":
        result_claim_id = _resolve_preference_claim(store, actor, item, at=at)
        return proposals.reject_proposal(
            store,
            proposal_id=proposal_id,
            gesture_id=gesture_id,
            actor=actor,
            reason_class="reject_as_preference",
            result_claim_id=result_claim_id,
            at=at,
        )
    if verb == "redirect":
        return proposals.redirect_proposal(
            store,
            proposal_id=proposal_id,
            gesture_id=gesture_id,
            actor=actor,
            note=item.get("redirect_note"),
            at=at,
        )
    if verb == "defer":
        return proposals.defer_proposal(
            store, proposal_id=proposal_id, gesture_id=gesture_id, actor=actor, at=at
        )
    if verb == "endorse":
        return proposals.endorse_flag(
            store, proposal_id=proposal_id, gesture_id=gesture_id, actor=actor, at=at
        )
    if verb == "dismiss":
        return proposals.dismiss_flag(
            store, proposal_id=proposal_id, gesture_id=gesture_id, actor=actor, at=at
        )
    raise InvariantViolation(f"unhandled verb: {verb!r}")


# The claim kind minted for a human-authored preferred phrasing (FA-1). A
# reject_as_preference records the human's own wording, so it lands as a
# preference claim authored by the same dashboard user who made the gesture.
_PREFERENCE_CLAIM_KIND = "preference"


def _resolve_preference_claim(
    store: TruthStore,
    actor: Actor,
    item: dict[str, Any],
    *,
    at: str | None,
) -> str:
    """Return the result claim id for a reject_as_preference decision (FA-1).

    An explicit result_claim_id names an existing claim and is used as is. Else
    the human-authored preference_text is minted as a new preference claim by
    the same dashboard user, and its id becomes the result claim. The caller
    prechecks that at least one of the two is present, so this never mints from
    empty text.
    """
    supplied = str(item.get("result_claim_id") or "").strip()
    if supplied:
        return supplied
    preference_text = str(item.get("preference_text") or "").strip()
    minted = store.propose_claim(
        proposition=preference_text,
        claim_kind=_PREFERENCE_CLAIM_KIND,
        actor=actor,
        created_at=at,
        status_at=at,
    )
    return minted.claim.id


def _precheck_inputs(proposal: Any, verb: str) -> str | None:
    """Return a reason to reject this verb before minting, else None.

    These mirror the engine's own structural guards so an item that cannot
    succeed returns error WITHOUT minting a gesture (S4: error mints nothing).
    """
    is_flag = proposal.replacement is None
    if verb == "confirm" and is_flag:
        return "a flag cannot be accepted, endorse or dismiss it"
    if verb in {"endorse", "dismiss"} and not is_flag:
        return f"{verb} is only valid on a flag"
    return None


def decide_one(
    store: TruthStore,
    document: DocumentRecord,
    actor: Actor,
    item: dict[str, Any],
    *,
    at: str | None = None,
) -> ItemOutcome:
    """Validate, mint one gesture, and commit one mark, independent of the rest."""
    proposal_id = str(item.get("proposal_id") or "").strip()
    verb = str(item.get("verb") or "").strip()
    supplied = str(item.get("canonical_sha256") or "").strip().lower()

    if verb not in _ALL_VERBS:
        return _error(proposal_id, verb, False, f"unsupported verb: {verb!r}")
    try:
        proposal = proposals.get_proposal(store, proposal_id)
    except InvariantViolation:
        return _error(proposal_id, verb, False, "proposal does not exist")

    base_ok = proposal.base_content_sha256 == document.content_sha256

    # I6 single-use binding: the shown hash must still equal the live payload.
    if supplied != proposal.canonical_sha256:
        return _stale_view(proposal_id, verb, base_ok)

    try:
        latest = proposals.latest_proposal_status(store, proposal_id)
    except InvariantViolation:
        return _error(proposal_id, verb, base_ok, "proposal has no status history")
    if latest.status != "open":
        return _error(proposal_id, verb, base_ok, f"proposal is {latest.status}, not open")

    if verb in _BASE_REQUIRED_VERBS and not base_ok:
        return _error(proposal_id, verb, base_ok, "stale_base")

    precheck = _precheck_inputs(proposal, verb)
    if precheck is not None:
        return _error(proposal_id, verb, base_ok, precheck)
    if verb == "edit_confirm" and not str(item.get("amend_content") or "").strip():
        return _error(proposal_id, verb, base_ok, "edit_confirm requires amend_content")
    if verb == "redirect" and not str(item.get("redirect_note") or "").strip():
        return _error(proposal_id, verb, base_ok, "redirect requires redirect_note")
    if verb == "reject_as_false" and not proposal.claim_refs_json and not str(
        item.get("negation_text") or ""
    ).strip():
        return _error(
            proposal_id,
            verb,
            base_ok,
            "reject_as_false requires the proposal to carry claim_refs or a "
            "negation_text (nothing to negate)",
        )
    if (
        verb == "reject_as_preference"
        and not str(item.get("result_claim_id") or "").strip()
        and not str(item.get("preference_text") or "").strip()
    ):
        return _error(
            proposal_id,
            verb,
            base_ok,
            "reject_as_preference requires a result_claim_id or a "
            "preference_text (nothing to record as the preferred phrasing)",
        )

    gesture = TruthLifecycle(store).mint_gesture(
        subject_ref=proposal_id,
        actor=actor,
        surface=DECISION_SURFACE,
        kind=_VERB_GESTURE_KIND[verb],
        displayed_payload_sha256=proposal.canonical_sha256,
        at=at,
    )
    try:
        decision = _dispatch(store, proposal, actor, verb, item, gesture.id, at)
    except _DECISION_ERRORS as exc:
        return _error(proposal_id, verb, base_ok, str(exc))

    entry = _base_result(proposal_id, verb, base_ok)
    entry["result"] = _RESULT_BY_VERB[verb]
    entry["gesture_id"] = decision.gesture.id if decision.gesture else gesture.id
    if decision.negation_claim is not None:
        entry["negation_claim_id"] = decision.negation_claim.id
    if verb == "reject_as_preference" and decision.result_claim_id is not None:
        entry["preference_claim_id"] = decision.result_claim_id
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "truth.doc_proposal_decided",
            {
                "document_id": document.id,
                "proposal_id": proposal_id,
                "verb": verb,
                "status": decision.status_event.status,
                "gesture_id": entry["gesture_id"],
            },
        )
    ]
    for expression in decision.expressions:
        events.append(
            (
                "truth.doc_expression_marked",
                {
                    "document_id": document.id,
                    "expression_id": expression.id,
                    "claim_ref": expression.claim_ref,
                },
            )
        )
    return ItemOutcome(result=entry, events=events, applied=verb in _APPLY_VERBS)


def _materialize(
    store: TruthStore,
    document: DocumentRecord,
    actor: Actor,
    materialize: dict[str, Any],
    *,
    at: str | None,
) -> str:
    """Verify the client-serialized markdown, write the file, record it."""
    rendered = materialize.get("rendered_markdown")
    declared = str(materialize.get("post_apply_content_sha256") or "").strip().lower()
    if not isinstance(rendered, str):
        raise InvariantViolation("materialize.rendered_markdown must be a string")
    # The client serializes (block-splice) and the server verifies only, it never
    # re-derives markdown (no server serializer, S5/C3).
    if sha256_text(rendered) != declared:
        raise MaterializeHashMismatch(declared)
    target = store.paths.root / document.path
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, rendered.encode("utf-8"))
    documents.record_materialization(
        store, document_id=document.id, content_sha256=declared, actor=actor, at=at
    )
    return declared


def apply_sitting(
    store: TruthStore,
    document: DocumentRecord,
    actor: Actor,
    *,
    items: list[dict[str, Any]],
    materialize: dict[str, Any] | None = None,
    at: str | None = None,
    deliver_routing: RoutingDeliver | None = None,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """Run one sitting: decide every mark, then materialize accepted edits.

    Returns the R5 response body plus the ordered post-commit events to emit.
    Raises MaterializeHashMismatch when a supplied materialize block is not
    self-consistent, so nothing commits and the route can answer 409.

    A redirect or endorse keeps the proposal open and must reach the proposing
    agent. When ``deliver_routing`` is supplied it is called with
    ``(verb, proposal_id, note)`` for each such committed decision, so the route
    can post the guidance into the document conversation. Delivery is a
    best-effort side effect: the decision is already durable in the ledger, so a
    delivery failure is logged and never rolls back the sitting.
    """
    # A self-inconsistent materialize block is a client integrity failure, not a
    # per-item staleness, so it is rejected before any decision commits.
    if materialize is not None:
        rendered = materialize.get("rendered_markdown")
        declared = str(materialize.get("post_apply_content_sha256") or "").strip().lower()
        if not isinstance(rendered, str) or sha256_text(rendered) != declared:
            raise MaterializeHashMismatch(declared)

    outcomes = [decide_one(store, document, actor, item, at=at) for item in items]
    results = [outcome.result for outcome in outcomes]
    events: list[tuple[str, dict[str, Any]]] = []
    for outcome in outcomes:
        events.extend(outcome.events)

    if deliver_routing is not None:
        for item, outcome in zip(items, outcomes):
            if outcome.result["result"] not in _ROUTED_RESULTS:
                continue
            try:
                deliver_routing(
                    outcome.result["verb"],
                    outcome.result["proposal_id"],
                    item.get("redirect_note"),
                )
            except Exception:  # noqa: BLE001 - delivery is best-effort post-commit
                logger.warning(
                    "routing delivery failed for %s on document %s",
                    outcome.result["verb"],
                    document.id,
                    exc_info=True,
                )

    materialize_block: dict[str, Any] | None = None
    applied_ids = [
        outcome.result["proposal_id"] for outcome in outcomes if outcome.applied
    ]
    if applied_ids and materialize is not None:
        new_sha256 = _materialize(store, document, actor, materialize, at=at)
        for outcome in outcomes:
            if outcome.applied:
                outcome.result["materialized"] = True
        materialize_block = {
            "file_path": str(store.paths.root / document.path),
            "new_file_sha256": new_sha256,
        }
        events.append(
            (
                "truth.doc_materialized",
                {"document_id": document.id, "file_sha256": new_sha256},
            )
        )
        for proposal_id in applied_ids:
            events.append(
                (
                    "truth.doc_proposal_applied",
                    {
                        "document_id": document.id,
                        "proposal_id": proposal_id,
                        "file_sha256": new_sha256,
                    },
                )
            )

    distinct = {result["result"] for result in results}
    partial = len(distinct) > 1 or bool(
        distinct & {"rejected_stale_view", "error"}
    )
    response = {
        "ok": True,
        "partial": partial,
        "results": results,
        "materialize": materialize_block,
    }
    return response, events


__all__ = [
    "DECISION_SURFACE",
    "ItemOutcome",
    "MaterializeHashMismatch",
    "apply_sitting",
    "decide_one",
]
