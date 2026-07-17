"""The edit-proposal ledger and its nine human decision paths (PRD section 6).

Agents PROPOSE (normal weight), never confirm. Every accept/reject/route is a
single-use human gesture bound to the proposal's canonical_sha256. Decisions are
recorded as append-only proposal_status_events with a typed decision verb, so an
agent can query what the human decided without parsing free text.

Durable inserts live in store.py. This module owns the decision policy and
composes inside one store.write_transaction per decision. Gesture verification
reuses the shipped lifecycle core unchanged (verify_and_consume_gesture). The
engine records status and verifies the client-posted post-apply hash, it never
mutates a Y.Doc (C3): the single dashboard client applies the accepted edit as a
local apply-origin transaction and submits marks plus the post-apply hash.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from work_buddy.truth import lifecycle as _lifecycle
from work_buddy.truth.contracts import (
    Actor,
    GestureError,
    InvariantViolation,
    TransitionError,
)
from work_buddy.truth.expressions import (
    _ensure_document_span_locked,
    _mark_expression_locked,
)
from work_buddy.truth.identity import (
    canonical_json,
    parse_truth_uri,
    sha256_text,
)
from work_buddy.truth.lifecycle import (
    TruthLifecycle,
    negated_proposition,
    rejection_binding_role,
)
from work_buddy.truth.store import (
    EXPRESSION_ROLES,
    ClaimLinkRecord,
    ClaimRecord,
    ExpressionRecord,
    GestureRecord,
    ProposalRecord,
    ProposalStatusEventRecord,
    TruthStore,
    _record_id,
    _require_text,
    _timestamp,
    _valid_digest,
    _valid_record_id,
)


_DEFAULT_NEGATION_CLAIM_KIND = "fact"


@dataclass(frozen=True, slots=True)
class ProposalDecisionResult:
    """The outcome of one human decision on a proposal."""

    proposal: ProposalRecord
    status_event: ProposalStatusEventRecord
    decision: str
    gesture: GestureRecord | None = None
    expressions: tuple[ExpressionRecord, ...] = ()
    negation_claim: ClaimRecord | None = None
    refutes_link: ClaimLinkRecord | None = None
    result_claim_id: str | None = None


# --------------------------------------------------------------------------
# Canonical hashing and dedup helpers.
# --------------------------------------------------------------------------


def _normalize_quote(quote: str) -> str:
    return " ".join(quote.split())


def _normalize_claim_refs(
    claim_refs: Sequence[Any] | None,
) -> list[dict[str, str]]:
    """Normalize claim_refs into the ONE frozen shape (S2/S7).

    A list of {claim:<local-id-or-uri>, role:quote|paraphrase|summary|
    instantiation}, role defaulting to instantiation.
    """
    if claim_refs is None:
        return []
    normalized: list[dict[str, str]] = []
    for item in claim_refs:
        if isinstance(item, Mapping):
            claim = item.get("claim")
            role = item.get("role", "instantiation")
        elif isinstance(item, str):
            claim = item
            role = "instantiation"
        else:
            raise InvariantViolation(
                "claim_refs items must be {claim, role} mappings or claim ids"
            )
        claim_value = _require_text(claim, "claim_refs[].claim")
        role_value = _require_text(role, "claim_refs[].role")
        if role_value not in EXPRESSION_ROLES:
            raise InvariantViolation(
                f"claim_refs[].role must be one of {sorted(EXPRESSION_ROLES)}"
            )
        normalized.append({"claim": claim_value, "role": role_value})
    return normalized


def proposal_canonical_sha256(
    *,
    document_id: str,
    base_content_sha256: str,
    selector: Any,
    quote_exact: str,
    replacement: str | None,
    rationale: str | None,
    tldr: str | None,
    claim_refs: Sequence[Any] | None,
) -> str:
    """Hash the EXACT reviewable content a gesture binds to (survives redaction)."""
    payload = {
        "document_id": document_id,
        "base_content_sha256": base_content_sha256,
        "selector": selector,
        "quote_exact": quote_exact,
        "replacement": replacement,
        "rationale": rationale,
        "tldr": tldr,
        "claim_refs": _normalize_claim_refs(claim_refs),
    }
    return sha256_text(canonical_json(payload))


def proposal_dedup_key(
    *,
    document_id: str,
    quote_exact: str,
    replacement: str | None,
) -> str:
    """Compute the (document, normalized quote, replacement hash) suppression key."""
    payload = {
        "document_id": document_id,
        "quote": _normalize_quote(quote_exact),
        "replacement_sha256": sha256_text(replacement or ""),
    }
    return sha256_text(canonical_json(payload))


# --------------------------------------------------------------------------
# Read paths.
# --------------------------------------------------------------------------


def get_proposal(
    store: TruthStore,
    proposal_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> ProposalRecord:
    """Return one proposal row or raise if unknown."""
    identifier = _valid_record_id(proposal_id, "proposal_id")
    if conn is not None:
        record = store._get_proposal_locked(conn, identifier)
    else:
        with store._read_connection() as read_conn:
            record = store._get_proposal_locked(read_conn, identifier)
    if record is None:
        raise InvariantViolation(f"proposal does not exist: {identifier}")
    return record


def latest_proposal_status(
    store: TruthStore,
    proposal_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> ProposalStatusEventRecord:
    """Return the latest proposal status by insertion sequence, never timestamp."""
    identifier = _valid_record_id(proposal_id, "proposal_id")
    if conn is not None:
        event = store._latest_proposal_status_locked(conn, identifier)
    else:
        with store._read_connection() as read_conn:
            event = store._latest_proposal_status_locked(read_conn, identifier)
    if event is None:
        raise InvariantViolation(f"proposal has no status history: {identifier}")
    return event


def open_proposals(
    store: TruthStore,
    *,
    document_id: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[ProposalRecord, ...]:
    """List proposals whose latest status is 'open' for one document."""
    identifier = _valid_record_id(document_id, "document_id")

    def _collect(read_conn: sqlite3.Connection) -> tuple[ProposalRecord, ...]:
        rows = read_conn.execute(
            "SELECT * FROM proposals WHERE document_id = ? ORDER BY created_at, id",
            (identifier,),
        ).fetchall()
        result: list[ProposalRecord] = []
        for row in rows:
            record = ProposalRecord(**dict(row))
            latest = read_conn.execute(
                "SELECT status FROM proposal_status_events WHERE proposal_id = ? "
                "ORDER BY seq DESC LIMIT 1",
                (record.id,),
            ).fetchone()
            if latest is not None and latest["status"] == "open":
                result.append(record)
        return tuple(result)

    if conn is not None:
        return _collect(conn)
    with store._read_connection() as read_conn:
        return _collect(read_conn)


# --------------------------------------------------------------------------
# Proposal creation.
# --------------------------------------------------------------------------


def _producer_meta_json(actor: Actor) -> str | None:
    from work_buddy.truth.contracts import validate_agent_producer_meta

    if actor.kind != "agent_run":
        return None
    validate_agent_producer_meta(actor.meta)
    data = {
        key: actor.meta[key]
        for key in ("model", "harness", "surface", "session_id")
        if actor.meta.get(key) is not None
    }
    return canonical_json(data) if data else None


def propose_edit(
    store: TruthStore,
    *,
    document_id: str,
    base_content_sha256: str,
    selector: Any,
    quote_exact: str,
    replacement: str | None = None,
    rationale: str | None = None,
    tldr: str | None = None,
    claim_refs: Sequence[Any] | None = None,
    actor: Actor,
    expires_at: str | None = None,
    at: str | None = None,
    proposal_id: str | None = None,
) -> ProposalRecord:
    """Append a quote-anchored edit (or a flag when replacement is None).

    Dedup-suppressed and stale-base-gated, agent producer identity enforced,
    returning the live match on a suppressed duplicate. claim_refs is the ONE
    frozen shape (S2/S7): a list of {claim, role}, role defaulting to
    instantiation, stored verbatim and carried through to accept-minting.
    """
    document_ref = _valid_record_id(document_id, "document_id")
    base_digest = _valid_digest(base_content_sha256, "base_content_sha256")
    quote = _require_text(quote_exact, "quote_exact")
    replacement_value = None if replacement is None else _require_text(
        replacement, "replacement"
    )
    rationale_value = None if rationale is None else _require_text(
        rationale, "rationale"
    )
    tldr_value = None if tldr is None else _require_text(tldr, "tldr")
    normalized_refs = _normalize_claim_refs(claim_refs)
    from work_buddy.truth.expressions import _serialize_selector_value

    selector_json = _serialize_selector_value(selector)
    span_sha256 = sha256_text(quote)
    canonical = proposal_canonical_sha256(
        document_id=document_ref,
        base_content_sha256=base_digest,
        selector=json.loads(selector_json),
        quote_exact=quote,
        replacement=replacement_value,
        rationale=rationale_value,
        tldr=tldr_value,
        claim_refs=normalized_refs,
    )
    dedup_key = proposal_dedup_key(
        document_id=document_ref,
        quote_exact=quote,
        replacement=replacement_value,
    )
    identifier = _record_id(proposal_id, "proposal id")
    created = _timestamp(at, "proposal created_at")
    expiry = None if expires_at is None else _timestamp(expires_at, "expires_at")
    meta_json = _producer_meta_json(actor)
    with store.write_transaction() as conn:
        if store._get_document_locked(conn, document_ref) is None:
            raise InvariantViolation(f"document does not exist: {document_ref}")
        suppressing = _find_suppressing_proposal_locked(
            store,
            conn,
            document_id=document_ref,
            dedup_key=dedup_key,
        )
        if suppressing is not None:
            return suppressing
        record = ProposalRecord(
            id=identifier,
            document_id=document_ref,
            base_content_sha256=base_digest,
            selector_json=selector_json,
            quote_exact=quote,
            span_sha256=span_sha256,
            replacement=replacement_value,
            rationale=rationale_value,
            tldr=tldr_value,
            claim_refs_json=(
                canonical_json(normalized_refs) if normalized_refs else None
            ),
            canonical_sha256=canonical,
            dedup_key=dedup_key,
            expires_at=expiry,
            created_at=created,
            created_by_kind=actor.kind,
            created_by_ref=actor.ref,
            meta_json=meta_json,
            redacted_at=None,
        )
        store._insert_proposal_locked(conn, record)
        store._insert_proposal_status_event_locked(
            conn,
            proposal_id=identifier,
            status="open",
            decision=None,
            actor=actor,
            basis_kind="rule",
            basis_ref=identifier,
            at=created,
        )
        return record


def _find_suppressing_proposal_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    document_id: str,
    dedup_key: str,
) -> ProposalRecord | None:
    """Return a live/rejected duplicate (suppresses), skipping expired ones.

    PRD section 6 dedup: suppress against live and rejected rows, but allow
    re-proposal once the prior duplicate has expired toward re-review.
    """
    rows = conn.execute(
        "SELECT * FROM proposals WHERE document_id = ? AND dedup_key = ? "
        "ORDER BY created_at, id",
        (document_id, dedup_key),
    ).fetchall()
    for row in rows:
        record = ProposalRecord(**dict(row))
        latest = store._latest_proposal_status_locked(conn, record.id)
        if latest is not None and latest.status != "expired":
            return record
    return None


# --------------------------------------------------------------------------
# Decision helpers.
# --------------------------------------------------------------------------


def _require_open_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    proposal_id: str,
) -> ProposalStatusEventRecord:
    latest = store._latest_proposal_status_locked(conn, proposal_id)
    if latest is None:
        raise InvariantViolation(f"proposal has no status history: {proposal_id}")
    if latest.status != "open":
        raise TransitionError(
            f"proposal is {latest.status}, not open, and cannot be decided"
        )
    return latest


def _redact_if_policy(
    store: TruthStore,
    conn: sqlite3.Connection,
    proposal_id: str,
    at: str,
) -> None:
    """Redact rejected/dismissed proposal content per gate.rejected_content."""
    if store.profile.gate.rejected_content == "redact":
        store._redact_proposal_content_locked(conn, proposal_id=proposal_id, at=at)


def _verify_and_consume(
    lifecycle: TruthLifecycle,
    conn: sqlite3.Connection,
    *,
    gesture_id: str,
    actor: Actor,
    proposal: ProposalRecord,
    allowed_kinds,
    expected_context_sha256: str | None,
    observed_at: str | None,
) -> GestureRecord:
    return lifecycle.verify_and_consume_gesture(
        gesture_id,
        actor=actor,
        subject_ref=proposal.id,
        payload_sha256=proposal.canonical_sha256,
        expected_context_sha256=expected_context_sha256,
        allowed_kinds=allowed_kinds,
        observed_at=observed_at,
        conn=conn,
    )


# --------------------------------------------------------------------------
# Accept (confirm / edit_confirm).
# --------------------------------------------------------------------------


def accept_proposal(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    amended_replacement: str | None = None,
    expected_context_sha256: str | None = None,
    observed_at: str | None = None,
    at: str | None = None,
) -> ProposalDecisionResult:
    """Consume a confirm (accept) or edit_confirm (amend) gesture, status 'applied'.

    On confirm ONLY, mint expression rows for the proposal's carried claim_refs,
    passing each ref's per-ref role through (S2). On edit_confirm (amend) SKIP
    expression minting (N10). The engine records status and (at the route) will
    verify the client-posted post-apply hash. It never mutates a Y.Doc.
    """
    identifier = _valid_record_id(proposal_id, "proposal_id")
    observed = _timestamp(observed_at or at, "accept observed_at")
    event_at = _timestamp(at or observed, "accept at")
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        proposal = store._get_proposal_locked(conn, identifier)
        if proposal is None:
            raise InvariantViolation(f"proposal does not exist: {identifier}")
        _require_open_locked(store, conn, identifier)
        document = store._get_document_locked(conn, proposal.document_id)
        if document is None:
            raise InvariantViolation("proposal references a missing document")
        if proposal.base_content_sha256 != document.content_sha256:
            raise TransitionError(
                "stale-base proposal cannot be applied, decidable only via "
                "reject or defer"
            )
        gesture = _verify_and_consume(
            lifecycle,
            conn,
            gesture_id=gesture_id,
            actor=actor,
            proposal=proposal,
            allowed_kinds=_lifecycle.PROPOSAL_ACCEPT_KINDS,
            expected_context_sha256=expected_context_sha256,
            observed_at=observed,
        )
        if gesture.surface not in store.profile.gate.confirmation_surfaces:
            raise GestureError(
                f"confirmation surface {gesture.surface!r} is not allowed by profile"
            )
        decision = gesture.kind
        expressions: tuple[ExpressionRecord, ...] = ()
        note: str | None = None
        if decision == "edit_confirm":
            if amended_replacement is None:
                raise TransitionError("edit_confirm (amend) requires amended_replacement")
            amended = _require_text(amended_replacement, "amended_replacement")
            note = f"amended_replacement_sha256:{sha256_text(amended)}"
        else:
            if amended_replacement is not None:
                raise TransitionError("confirm (accept) cannot carry amended_replacement")
            if proposal.replacement is None:
                raise TransitionError("a flag cannot be accepted, endorse or dismiss it")
            expressions = _mint_expressions_locked(
                store,
                conn,
                proposal=proposal,
                actor=actor,
                at=event_at,
            )
        status_event = store._insert_proposal_status_event_locked(
            conn,
            proposal_id=identifier,
            status="applied",
            decision=decision,
            actor=actor,
            basis_kind="gesture",
            basis_ref=gesture.id,
            note=note,
            at=event_at,
        )
        refreshed = store._get_proposal_locked(conn, identifier)
        assert refreshed is not None
        return ProposalDecisionResult(
            proposal=refreshed,
            status_event=status_event,
            decision=decision,
            gesture=gesture,
            expressions=expressions,
        )


def _mint_expressions_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    proposal: ProposalRecord,
    actor: Actor,
    at: str,
) -> tuple[ExpressionRecord, ...]:
    refs = (
        json.loads(proposal.claim_refs_json) if proposal.claim_refs_json else []
    )
    if not refs:
        return ()
    span = _ensure_document_span_locked(
        store,
        conn,
        document_id=proposal.document_id,
        selector=json.loads(proposal.selector_json),
        quote_exact=proposal.replacement or proposal.quote_exact,
        actor=actor,
        at=at,
    )
    minted: list[ExpressionRecord] = []
    for ref in refs:
        minted.append(
            _mark_expression_locked(
                store,
                conn,
                document_span_id=span.id,
                claim_ref=str(ref["claim"]),
                role=str(ref.get("role", "instantiation")),
                actor=actor,
                at=at,
            )
        )
    return tuple(minted)


# --------------------------------------------------------------------------
# Reject (reject_plain / reject_as_preference) and reject_as_false.
# --------------------------------------------------------------------------


def reject_proposal(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    reason_class: str,
    result_claim_id: str | None = None,
    expected_context_sha256: str | None = None,
    observed_at: str | None = None,
    at: str | None = None,
) -> ProposalDecisionResult:
    """Apply a reject_plain or reject_as_preference rejection, close and redact.

    reject_as_preference records the human's preferred phrasing via
    result_claim_id. reject_as_false is NOT handled here (see
    decide_reject_as_false).
    """
    identifier = _valid_record_id(proposal_id, "proposal_id")
    rejection = _require_text(reason_class, "reason_class")
    if rejection not in {"reject_plain", "reject_as_preference"}:
        raise TransitionError(
            "reject_proposal handles reject_plain or reject_as_preference only"
        )
    observed = _timestamp(observed_at or at, "reject observed_at")
    event_at = _timestamp(at or observed, "reject at")
    result_ref = None
    note: str | None = None
    if rejection == "reject_as_preference":
        if result_claim_id is None:
            raise TransitionError(
                "reject_as_preference requires a result_claim_id"
            )
        result_ref = _valid_record_id(result_claim_id, "result_claim_id")
        note = f"reject_as_preference:result_claim={result_ref}"
    elif result_claim_id is not None:
        raise TransitionError("reject_plain cannot carry a result claim")
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        proposal = store._get_proposal_locked(conn, identifier)
        if proposal is None:
            raise InvariantViolation(f"proposal does not exist: {identifier}")
        _require_open_locked(store, conn, identifier)
        if rejection == "reject_as_preference" and (
            store._get_claim_locked(conn, result_ref) is None
        ):
            raise InvariantViolation(f"result claim does not exist: {result_ref}")
        gesture = _verify_and_consume(
            lifecycle,
            conn,
            gesture_id=gesture_id,
            actor=actor,
            proposal=proposal,
            allowed_kinds={rejection},
            expected_context_sha256=expected_context_sha256,
            observed_at=observed,
        )
        status_event = store._insert_proposal_status_event_locked(
            conn,
            proposal_id=identifier,
            status="closed",
            decision=rejection,
            actor=actor,
            basis_kind="gesture",
            basis_ref=gesture.id,
            note=note,
            at=event_at,
        )
        _redact_if_policy(store, conn, identifier, event_at)
        refreshed = store._get_proposal_locked(conn, identifier)
        assert refreshed is not None
        return ProposalDecisionResult(
            proposal=refreshed,
            status_event=status_event,
            decision=rejection,
            gesture=gesture,
            result_claim_id=result_ref,
        )


def _resolve_local_claim(store: TruthStore, conn, claim_ref: str) -> ClaimRecord | None:
    ref = _require_text(claim_ref, "claim_ref")
    if ref.startswith("wb-truth://"):
        parsed = parse_truth_uri(ref)
        if parsed.kind != "claim" or parsed.store_id != store.store_id:
            return None
        return store._get_claim_locked(conn, parsed.record_id)
    return store._get_claim_locked(conn, _valid_record_id(ref, "claim_ref"))


def decide_reject_as_false(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    negation_text: str | None = None,
    expected_context_sha256: str | None = None,
    observed_at: str | None = None,
    at: str | None = None,
) -> ProposalDecisionResult:
    """The dedicated proposal-specific reject_as_false decision path (S3).

    Valid only when the proposal carries claim_refs (the DETERMINISTIC negation
    of the referenced claim is minted via negated_proposition) OR the human
    supplies explicit negation_text (the negation claim's proposition VERBATIM).
    Composes the negation claim, a refutes link, and the proposal closure inside
    ONE write_transaction under ONE gesture bound to the proposal's
    canonical_sha256. HARD-FAILS with TransitionError when NEITHER claim_refs
    nor negation_text is present (adjudication 6, never a silent downgrade to
    reject_plain).
    """
    identifier = _valid_record_id(proposal_id, "proposal_id")
    observed = _timestamp(observed_at or at, "reject observed_at")
    event_at = _timestamp(at or observed, "reject at")
    supplied_negation = None if negation_text is None else _require_text(
        negation_text, "negation_text"
    )
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        proposal = store._get_proposal_locked(conn, identifier)
        if proposal is None:
            raise InvariantViolation(f"proposal does not exist: {identifier}")
        _require_open_locked(store, conn, identifier)
        refs = (
            json.loads(proposal.claim_refs_json) if proposal.claim_refs_json else []
        )
        if not refs and supplied_negation is None:
            raise TransitionError(
                "reject_as_false requires the proposal to carry claim_refs or the "
                "human to supply negation_text (nothing to negate)"
            )
        gesture = _verify_and_consume(
            lifecycle,
            conn,
            gesture_id=gesture_id,
            actor=actor,
            proposal=proposal,
            allowed_kinds={"reject_as_false"},
            expected_context_sha256=expected_context_sha256,
            observed_at=observed,
        )

        referenced: ClaimRecord | None = None
        if refs:
            referenced = _resolve_local_claim(store, conn, str(refs[0]["claim"]))
            if referenced is None:
                raise InvariantViolation(
                    "reject_as_false requires the referenced claim to resolve "
                    "in this store"
                )
        if supplied_negation is not None:
            proposition = supplied_negation
            claim_kind = (
                referenced.claim_kind if referenced is not None
                else _DEFAULT_NEGATION_CLAIM_KIND
            )
            scope = referenced.scope if referenced is not None else "store"
        else:
            assert referenced is not None
            proposition = negated_proposition(referenced.proposition)
            claim_kind = referenced.claim_kind
            scope = referenced.scope

        negation = store.propose_claim(
            proposition=proposition,
            claim_kind=claim_kind,
            actor=actor,
            scope=scope,
            created_at=event_at,
            status_at=event_at,
            conn=conn,
        ).claim
        # One gesture covers both the rejection and the confirmed negation. The
        # negation is confirmed under the same proposal-bound gesture, so the
        # confirm-gesture uniqueness index binds this gesture to exactly one
        # confirmed claim.
        store._insert_status_event_locked(
            conn,
            claim_id=negation.id,
            status="confirmed",
            actor=actor,
            basis_kind="gesture",
            basis_ref=gesture.id,
            note="reject_as_false",
            at=event_at,
        )
        refutes: ClaimLinkRecord | None = None
        if referenced is not None:
            refutes = store.add_link(
                from_claim_id=negation.id,
                link_type="refutes",
                to_kind="claim",
                to_ref=referenced.id,
                actor=actor,
                role=rejection_binding_role(
                    rejection_class="reject_as_false",
                    source_canonical_sha256=referenced.canonical_sha256,
                    result_canonical_sha256=negation.canonical_sha256,
                ),
                created_at=event_at,
                conn=conn,
            )
        status_event = store._insert_proposal_status_event_locked(
            conn,
            proposal_id=identifier,
            status="closed",
            decision="reject_as_false",
            actor=actor,
            basis_kind="gesture",
            basis_ref=gesture.id,
            note=f"reject_as_false:negation_claim={negation.id}",
            at=event_at,
        )
        _redact_if_policy(store, conn, identifier, event_at)
        refreshed = store._get_proposal_locked(conn, identifier)
        assert refreshed is not None
        return ProposalDecisionResult(
            proposal=refreshed,
            status_event=status_event,
            decision="reject_as_false",
            gesture=gesture,
            negation_claim=negation,
            refutes_link=refutes,
        )


# --------------------------------------------------------------------------
# Routing decisions that keep the proposal open.
# --------------------------------------------------------------------------


def _route_open(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    gesture_kind: str,
    decision: str,
    note: str | None,
    require_flag: bool,
    expected_context_sha256: str | None,
    observed_at: str | None,
    at: str | None,
) -> ProposalDecisionResult:
    identifier = _valid_record_id(proposal_id, "proposal_id")
    observed = _timestamp(observed_at or at, "route observed_at")
    event_at = _timestamp(at or observed, "route at")
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        proposal = store._get_proposal_locked(conn, identifier)
        if proposal is None:
            raise InvariantViolation(f"proposal does not exist: {identifier}")
        _require_open_locked(store, conn, identifier)
        if require_flag and proposal.replacement is not None:
            raise TransitionError(f"{decision} is only valid on a flag (no replacement)")
        gesture = _verify_and_consume(
            lifecycle,
            conn,
            gesture_id=gesture_id,
            actor=actor,
            proposal=proposal,
            allowed_kinds={gesture_kind},
            expected_context_sha256=expected_context_sha256,
            observed_at=observed,
        )
        status_event = store._insert_proposal_status_event_locked(
            conn,
            proposal_id=identifier,
            status="open",
            decision=decision,
            actor=actor,
            basis_kind="gesture",
            basis_ref=gesture.id,
            note=note,
            at=event_at,
        )
        return ProposalDecisionResult(
            proposal=proposal,
            status_event=status_event,
            decision=decision,
            gesture=gesture,
        )


def redirect_proposal(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    note: str,
    expected_context_sha256: str | None = None,
    observed_at: str | None = None,
    at: str | None = None,
) -> ProposalDecisionResult:
    """Consume a redirect gesture, record typed guidance, keep the proposal open."""
    guidance = _require_text(note, "note")
    return _route_open(
        store,
        proposal_id=proposal_id,
        gesture_id=gesture_id,
        actor=actor,
        gesture_kind="redirect",
        decision="redirect",
        note=guidance,
        require_flag=False,
        expected_context_sha256=expected_context_sha256,
        observed_at=observed_at,
        at=at,
    )


def defer_proposal(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    expected_context_sha256: str | None = None,
    observed_at: str | None = None,
    at: str | None = None,
) -> ProposalDecisionResult:
    """Consume a defer gesture, park the proposal open."""
    return _route_open(
        store,
        proposal_id=proposal_id,
        gesture_id=gesture_id,
        actor=actor,
        gesture_kind="defer",
        decision="defer",
        note=None,
        require_flag=False,
        expected_context_sha256=expected_context_sha256,
        observed_at=observed_at,
        at=at,
    )


def endorse_flag(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    expected_context_sha256: str | None = None,
    observed_at: str | None = None,
    at: str | None = None,
) -> ProposalDecisionResult:
    """Consume an endorse gesture on a FLAG, keep it open, route to the agent.

    A drafted fix returns as a NEW linked proposal, never an auto-apply (AOV).
    """
    return _route_open(
        store,
        proposal_id=proposal_id,
        gesture_id=gesture_id,
        actor=actor,
        gesture_kind="endorse",
        decision="endorse",
        note="route_to_proposing_agent",
        require_flag=True,
        expected_context_sha256=expected_context_sha256,
        observed_at=observed_at,
        at=at,
    )


def dismiss_flag(
    store: TruthStore,
    *,
    proposal_id: str,
    gesture_id: str,
    actor: Actor,
    expected_context_sha256: str | None = None,
    observed_at: str | None = None,
    at: str | None = None,
) -> ProposalDecisionResult:
    """Consume a reject_plain gesture on a FLAG, close it with no truth stance."""
    identifier = _valid_record_id(proposal_id, "proposal_id")
    observed = _timestamp(observed_at or at, "dismiss observed_at")
    event_at = _timestamp(at or observed, "dismiss at")
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        proposal = store._get_proposal_locked(conn, identifier)
        if proposal is None:
            raise InvariantViolation(f"proposal does not exist: {identifier}")
        _require_open_locked(store, conn, identifier)
        if proposal.replacement is not None:
            raise TransitionError("dismiss is only valid on a flag (no replacement)")
        gesture = _verify_and_consume(
            lifecycle,
            conn,
            gesture_id=gesture_id,
            actor=actor,
            proposal=proposal,
            allowed_kinds={"reject_plain"},
            expected_context_sha256=expected_context_sha256,
            observed_at=observed,
        )
        status_event = store._insert_proposal_status_event_locked(
            conn,
            proposal_id=identifier,
            status="closed",
            decision="dismiss",
            actor=actor,
            basis_kind="gesture",
            basis_ref=gesture.id,
            at=event_at,
        )
        _redact_if_policy(store, conn, identifier, event_at)
        refreshed = store._get_proposal_locked(conn, identifier)
        assert refreshed is not None
        return ProposalDecisionResult(
            proposal=refreshed,
            status_event=status_event,
            decision="dismiss",
            gesture=gesture,
        )


def expire_proposal(
    store: TruthStore,
    *,
    proposal_id: str,
    basis_kind: str,
    basis_ref: str | None = None,
    actor: Actor,
    at: str | None = None,
) -> ProposalDecisionResult:
    """Expire a proposal by rule or sweep TOWARD RE-REVIEW (never acceptance)."""
    identifier = _valid_record_id(proposal_id, "proposal_id")
    basis = _require_text(basis_kind, "basis_kind")
    if basis not in {"rule", "sweep"}:
        raise TransitionError("expiry basis_kind must be rule or sweep")
    event_at = _timestamp(at, "expire at")
    with store.write_transaction() as conn:
        proposal = store._get_proposal_locked(conn, identifier)
        if proposal is None:
            raise InvariantViolation(f"proposal does not exist: {identifier}")
        _require_open_locked(store, conn, identifier)
        status_event = store._insert_proposal_status_event_locked(
            conn,
            proposal_id=identifier,
            status="expired",
            decision=None,
            actor=actor,
            basis_kind=basis,
            basis_ref=basis_ref or basis,
            at=event_at,
        )
        return ProposalDecisionResult(
            proposal=proposal,
            status_event=status_event,
            decision="expire",
        )


__all__ = [
    "ProposalDecisionResult",
    "accept_proposal",
    "decide_reject_as_false",
    "defer_proposal",
    "dismiss_flag",
    "endorse_flag",
    "expire_proposal",
    "get_proposal",
    "latest_proposal_status",
    "open_proposals",
    "propose_edit",
    "proposal_canonical_sha256",
    "proposal_dedup_key",
    "redirect_proposal",
    "reject_proposal",
]
