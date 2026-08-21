"""Recoverable two-phase application of human Co-work review sittings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from work_buddy.cowork import materialization, provenance, sittings
from work_buddy.cowork.lifecycle_state import inspect_lifecycle_state
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.proposal_applicability import (
    CurrentProjection,
    assess_proposal_applicability,
    load_current_projection,
)
from work_buddy.cowork.verify_coordination import record_review_application
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import canonical_json, new_id, sha256_text
from work_buddy.truth.store import DocumentRecord, TruthStore, _valid_digest


INTENT_TTL = timedelta(minutes=15)
_ALLOWED_ITEM_KEYS = frozenset(
    {
        "proposal_id",
        "verb",
        "canonical_sha256",
        "amend_content",
        "negation_text",
        "result_claim_id",
        "preference_text",
        "redirect_note",
    }
)


class SittingError(InvariantViolation):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SittingIntent:
    id: str
    idempotency_key: str
    actor_ref: str
    document_id: str
    request_sha256: str
    state: str
    expected_file_sha256: str
    expected_structured_head_sha256: str
    expected_snapshot_sha256: str
    admitted: tuple[dict[str, Any], ...]
    failed: tuple[dict[str, Any], ...]
    has_apply: bool
    created_at: str
    expires_at: str
    receipt: dict[str, Any] | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _expiry() -> str:
    return (datetime.now(timezone.utc) + INTENT_TTL).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _expired(value: str) -> bool:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(
        timezone.utc
    )


def _actor_ref(actor: Actor) -> str:
    if actor.kind != "human" or not actor.ref:
        raise SittingError("human_actor_required", "a dashboard human actor is required")
    return actor.ref


def _intent(row: sqlite3.Row) -> SittingIntent:
    return SittingIntent(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        actor_ref=row["actor_ref"],
        document_id=row["document_id"],
        request_sha256=row["request_sha256"],
        state=row["state"],
        expected_file_sha256=row["expected_file_sha256"],
        expected_structured_head_sha256=row["expected_structured_head_sha256"],
        expected_snapshot_sha256=row["expected_snapshot_sha256"],
        admitted=tuple(json.loads(row["admitted_items_json"])),
        failed=tuple(json.loads(row["failed_items_json"])),
        has_apply=bool(row["has_apply"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        receipt=None if row["receipt_json"] is None else json.loads(row["receipt_json"]),
    )


def _load_intent(
    store: TruthStore,
    intent_id: str,
    actor_ref: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> SittingIntent:
    if conn is None:
        with store._read_connection() as read_conn:
            row = read_conn.execute(
                "SELECT * FROM cowork_sitting_intents WHERE id = ?", (intent_id,)
            ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM cowork_sitting_intents WHERE id = ?", (intent_id,)
        ).fetchone()
    if row is None:
        raise SittingError("intent_not_found", "sitting intent does not exist", status=404)
    value = _intent(row)
    if value.actor_ref != actor_ref:
        raise SittingError("intent_actor_mismatch", "sitting intent belongs to another actor", status=403)
    return value


def _item_error(
    proposal_id: str,
    verb: str,
    message: str,
    *,
    base_ok: bool = False,
) -> dict[str, Any]:
    result = sittings._base_result(proposal_id, verb, base_ok)
    result["error"] = message
    return result


def _admit_item(
    store: TruthStore,
    document: DocumentRecord,
    item: Mapping[str, Any],
    *,
    structured_head_sha256: str,
    current_projection: CurrentProjection | None,
    projection_unavailable_reason: str,
    conn: sqlite3.Connection,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    proposal_id = str(item.get("proposal_id") or "").strip()
    verb = str(item.get("verb") or "").strip()
    if set(item) - _ALLOWED_ITEM_KEYS:
        return None, _item_error(proposal_id, verb, "unsupported item fields")
    if verb not in sittings._ALL_VERBS:
        return None, _item_error(proposal_id, verb, f"unsupported verb: {verb!r}")
    try:
        proposal = proposals.get_proposal(store, proposal_id, conn=conn)
    except InvariantViolation:
        return None, _item_error(proposal_id, verb, "proposal does not exist")
    if proposal.document_id != document.id:
        return None, _item_error(proposal_id, verb, "proposal belongs to another document")
    applicability = assess_proposal_applicability(
        proposal,
        document,
        structured_head_sha256=structured_head_sha256,
        current_projection=current_projection,
        projection_unavailable_reason=projection_unavailable_reason,
    )
    base_ok = applicability.applicable
    supplied = str(item.get("canonical_sha256") or "").strip().lower()
    if supplied != proposal.canonical_sha256:
        result = _item_error(proposal_id, verb, "canonical_sha256 no longer matches the shown proposal", base_ok=base_ok)
        result["result"] = "rejected_stale_view"
        return None, result
    try:
        latest = proposals.latest_proposal_status(store, proposal_id, conn=conn)
    except InvariantViolation:
        return None, _item_error(proposal_id, verb, "proposal has no status history", base_ok=base_ok)
    if latest.status != "open":
        return None, _item_error(proposal_id, verb, f"proposal is {latest.status}, not open", base_ok=base_ok)
    if verb in sittings._BASE_REQUIRED_VERBS and not base_ok:
        return None, _item_error(
            proposal_id,
            verb,
            applicability.reason,
            base_ok=False,
        )
    precheck = sittings._precheck_inputs(proposal, verb)
    if precheck is not None:
        return None, _item_error(proposal_id, verb, precheck, base_ok=base_ok)
    if verb == "edit_confirm":
        amend_error = sittings._amend_content_error(item)
        if amend_error is not None:
            return None, _item_error(proposal_id, verb, amend_error, base_ok=base_ok)
    if verb == "redirect" and not str(item.get("redirect_note") or "").strip():
        return None, _item_error(proposal_id, verb, "redirect requires redirect_note", base_ok=base_ok)
    if verb == "reject_as_false" and not proposal.claim_refs_json and not str(item.get("negation_text") or "").strip():
        return None, _item_error(proposal_id, verb, "reject_as_false has nothing to negate", base_ok=base_ok)
    if verb == "reject_as_preference":
        claim_id = str(item.get("result_claim_id") or "").strip()
        preference = str(item.get("preference_text") or "").strip()
        if not claim_id and not preference:
            return None, _item_error(proposal_id, verb, "reject_as_preference requires a result claim or preference text", base_ok=base_ok)
        if claim_id and store._get_claim_locked(conn, claim_id) is None:
            return None, _item_error(proposal_id, verb, "result claim does not exist", base_ok=base_ok)
    admitted = dict(item)
    admitted["_applicability"] = applicability.to_wire()
    return admitted, None


def prepare_sitting(
    store: TruthStore,
    *,
    document_id: str,
    actor: Actor,
    items: list[dict[str, Any]],
    expected_file_sha256: str,
    expected_structured_head_sha256: str,
    idempotency_key: str,
) -> tuple[SittingIntent, bool]:
    actor_ref = _actor_ref(actor)
    if not isinstance(items, list) or not items:
        raise SittingError("invalid_items", "items must be a non-empty list")
    if any(not isinstance(item, dict) for item in items):
        raise SittingError("invalid_items", "every sitting item must be an object")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise SittingError("invalid_idempotency_key", "a bounded idempotency_key is required")
    expected_file = _valid_digest(expected_file_sha256, "expected_file_sha256")
    expected_head = _valid_digest(
        expected_structured_head_sha256, "expected_structured_head_sha256"
    )
    request_sha = sha256_text(
        canonical_json(
            {
                "document_id": document_id,
                "items": items,
                "expected_file_sha256": expected_file,
                "expected_structured_head_sha256": expected_head,
            }
        )
    )
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM cowork_sitting_intents WHERE actor_ref = ? AND idempotency_key = ?",
            (actor_ref, key),
        ).fetchone()
    if row is not None:
        prior = _intent(row)
        if prior.request_sha256 != request_sha:
            raise SittingError("idempotency_conflict", "idempotency key was used for another sitting", status=409)
        return prior, False

    initial = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial.path),
    ):
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise SittingError("document_retired", "retired documents cannot be reviewed", status=409)
        if not document_surface_allowed(store, document):
            raise SittingError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        state = inspect_lifecycle_state(store, document)
        if state.initialization_state != "ready" or state.structured_head_sha256 is None:
            raise SittingError("document_not_ready", f"document is {state.initialization_state}", status=409)
        expected_projection = (
            document.content_sha256
            if documents.source_is_detached(document)
            else state.current_file_sha256
        )
        if expected_projection != expected_file:
            raise SittingError(
                "stale_file",
                (
                    "Co-work projection changed before sitting prepare"
                    if documents.source_is_detached(document)
                    else "Markdown file changed before sitting prepare"
                ),
                status=409,
                details={"current_file_sha256": expected_projection},
            )
        if state.structured_head_sha256 != expected_head:
            raise SittingError("stale_structured_head", "structured document changed before sitting prepare", status=409, details={"server_structured_head_sha256": state.structured_head_sha256})
        if document.ydoc_snapshot_sha256 is None:
            raise SittingError(
                "document_not_ready",
                "The structured document is not initialized.",
                status=409,
            )
        if sittings.DECISION_SURFACE not in store.profile.gate.confirmation_surfaces:
            raise SittingError("policy_forbidden", "dashboard review is not an allowed confirmation surface", status=403)

        admitted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        seen: set[str] = set()
        with store._read_connection() as conn:
            current_projection, projection_reason = load_current_projection(
                store,
                document,
                structured_head_sha256=expected_head,
                conn=conn,
            )
            for index, item in enumerate(items):
                proposal_id = str(item.get("proposal_id") or "").strip()
                if proposal_id in seen:
                    failed.append({"index": index, "result": _item_error(proposal_id, str(item.get("verb") or ""), "duplicate proposal in sitting")})
                    continue
                seen.add(proposal_id)
                accepted, error = _admit_item(
                    store,
                    document,
                    item,
                    structured_head_sha256=expected_head,
                    current_projection=current_projection,
                    projection_unavailable_reason=projection_reason,
                    conn=conn,
                )
                if error is not None:
                    failed.append({"index": index, "result": error})
                else:
                    admitted.append({"index": index, "item": accepted})
        now = _now()
        intent_id = new_id()
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO cowork_sitting_intents (id, idempotency_key, actor_ref, document_id, request_sha256, state, expected_file_sha256, expected_structured_head_sha256, expected_snapshot_sha256, admitted_items_json, failed_items_json, has_apply, created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    key,
                    actor_ref,
                    document.id,
                    request_sha,
                    expected_file,
                    expected_head,
                    document.ydoc_snapshot_sha256,
                    canonical_json(admitted),
                    canonical_json(failed),
                    int(any(entry["item"]["verb"] in sittings._APPLY_VERBS for entry in admitted)),
                    now,
                    now,
                    _expiry(),
                ),
            )
        return _load_intent(store, intent_id, actor_ref), True


def _commit_decisions(
    store: TruthStore,
    document: DocumentRecord,
    actor: Actor,
    intent: SittingIntent,
    conn: sqlite3.Connection,
    *,
    at: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    outcomes: list[tuple[int, sittings.ItemOutcome]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    for entry in intent.admitted:
        application_proof = entry["item"].get("_applicability")
        outcome = sittings.decide_one(
            store,
            document,
            actor,
            dict(entry["item"]),
            at=at,
            conn=conn,
            current_structured_head_sha256=intent.expected_structured_head_sha256,
            applicability_prevalidated=(
                isinstance(application_proof, Mapping)
                and application_proof.get("status") == "applicable"
            ),
        )
        if outcome.result["result"] in {"error", "rejected_stale_view"}:
            raise SittingError(
                "decision_conflict",
                "an admitted proposal changed before sitting commit",
                status=409,
                retryable=True,
                details={"proposal_id": outcome.result["proposal_id"], "error": outcome.result["error"]},
            )
        outcomes.append((int(entry["index"]), outcome))
        events.extend(outcome.events)
    ordered = [(index, outcome.result) for index, outcome in outcomes]
    ordered.extend((int(entry["index"]), dict(entry["result"])) for entry in intent.failed)
    ordered.sort(key=lambda pair: pair[0])
    return [result for _, result in ordered], events


def _routing_deliveries(intent: SittingIntent) -> list[dict[str, Any]]:
    return [
        {
            "delivery_id": sha256_text(
                canonical_json(
                    {
                        "kind": "cowork-sitting-routing",
                        "intent_id": intent.id,
                        "index": entry["index"],
                    }
                )
            ),
            "verb": entry["item"]["verb"],
            "proposal_id": entry["item"]["proposal_id"],
            "note": entry["item"].get("redirect_note"),
        }
        for entry in intent.admitted
        if entry["item"]["verb"] in {"redirect", "endorse"}
    ]


def _event_records(
    intent_id: str,
    events: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    return [
        {
            "event_id": sha256_text(
                canonical_json(
                    {
                        "kind": "cowork-sitting-event",
                        "intent_id": intent_id,
                        "index": index,
                    }
                )
            ),
            "event_type": event_type,
            "data": data,
        }
        for index, (event_type, data) in enumerate(events)
    ]


def _receipt_events(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = receipt.get("post_commit_events", [])
    return [dict(item) for item in value if isinstance(item, dict)]


def commit_sitting(
    store: TruthStore,
    *,
    document_id: str,
    intent_id: str,
    actor: Actor,
    snapshot: bytes | None = None,
    snapshot_sha256: str | None = None,
    rendered_markdown: str | None = None,
    rendered_sha256: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    actor_ref = _actor_ref(actor)
    intent = _load_intent(store, intent_id, actor_ref)
    if intent.document_id != document_id:
        raise SittingError("intent_document_mismatch", "sitting intent belongs to another document", status=409)
    if intent.state == "committed" and intent.receipt is not None:
        try:
            materialization_intent_id = intent.receipt.get(
                "materialization_intent_id"
            )
            if isinstance(materialization_intent_id, str):
                materialization.recover_materialization_intent(
                    store, materialization_intent_id
                )
            else:
                ydoc_store.recover_compaction(store, document_id=document_id)
        except (
            ydoc_store.CompactionRecoveryRequired,
            materialization.MaterializationError,
        ) as exc:
            raise SittingError(
                "recovery_required",
                str(exc),
                status=409,
                retryable=True,
            ) from exc
        return intent.receipt, _receipt_events(intent.receipt)
    if intent.state != "prepared":
        raise SittingError("intent_not_committable", f"sitting intent is {intent.state}", status=409)
    if _expired(intent.expires_at):
        raise SittingError("intent_expired", "sitting intent expired; prepare again", status=409)
    if intent.has_apply:
        if snapshot is None or rendered_markdown is None:
            raise SittingError("commit_payload_required", "accepted edits require a complete snapshot and rendered Markdown")
        new_snapshot_sha = _valid_digest(snapshot_sha256, "snapshot_sha256")
        rendered_sha = _valid_digest(rendered_sha256, "rendered_sha256")
        document = documents.get_document(store, document_id)

        def _lock_preflight() -> Mapping[str, Any] | None:
            current_intent = _load_intent(store, intent.id, actor_ref)
            if current_intent.state == "committed" and current_intent.receipt is not None:
                materialization_intent_id = current_intent.receipt.get(
                    "materialization_intent_id"
                )
                if isinstance(materialization_intent_id, str):
                    materialization.recover_materialization_intent_locked(
                        store, materialization_intent_id
                    )
                else:
                    ydoc_store.recover_compaction_locked(
                        store, document_id=document_id
                    )
                return current_intent.receipt
            if current_intent.state != "prepared":
                raise SittingError(
                    "intent_not_committable",
                    f"sitting intent is {current_intent.state}",
                    status=409,
                )
            if _expired(current_intent.expires_at):
                raise SittingError(
                    "intent_expired",
                    "sitting intent expired; prepare again",
                    status=409,
                )
            return None

        def _commit_callback(
            conn: sqlite3.Connection,
            projection_receipt: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            current_intent = _load_intent(store, intent.id, actor_ref, conn=conn)
            if current_intent.state != "prepared":
                raise SittingError("intent_not_committable", "sitting intent changed before commit", status=409)
            results, events = _commit_decisions(
                store, document, actor, current_intent, conn, at=str(projection_receipt["materialized_at"])
            )
            result_by_proposal = {
                str(result.get("proposal_id")): result for result in results
            }
            for entry in current_intent.admitted:
                item = entry["item"]
                if item.get("verb") != "confirm":
                    continue
                proposal_id = str(item["proposal_id"])
                result = result_by_proposal.get(proposal_id)
                if result is None or result.get("result") != "applied":
                    continue
                accepted = proposals.get_proposal(
                    store,
                    proposal_id,
                    conn=conn,
                )
                try:
                    attestations = (
                        provenance.record_proposal_acceptance_attestations_locked(
                            store,
                            conn,
                            proposal=accepted,
                            gesture_id=str(result["gesture_id"]),
                            actor=actor,
                            target_structured_head_sha256=str(
                                projection_receipt["structured_head_sha256"]
                            ),
                            rendered_projection=rendered_markdown,
                            at=str(projection_receipt["materialized_at"]),
                        )
                    )
                except provenance.ProposalAcceptanceProvenanceError as exc:
                    raise SittingError(
                        "proposal_provenance_unsafe",
                        str(exc),
                        status=409,
                    ) from exc
                result["provenance_attestation_ids"] = [
                    attestation.id for attestation in attestations
                ]
                events.extend(
                    (
                        "truth.doc_provenance_attested",
                        {
                            "document_id": document.id,
                            "attestation_id": attestation.id,
                            "document_span_id": attestation.document_span_id,
                            "target_structured_head_sha256": (
                                attestation.target_structured_head_sha256
                            ),
                            "basis_kind": attestation.basis_kind,
                        },
                    )
                    for attestation in attestations
                )
            record_review_application(
                store,
                application_id=current_intent.id,
                document_id=document.id,
                applied_proposal_ids=[
                    str(result["proposal_id"])
                    for result in results
                    if result.get("result") == "applied"
                ],
                committed_at=str(projection_receipt["materialized_at"]),
                actor=actor,
                conn=conn,
            )
            event_records = _event_records(intent.id, events)
            additions = {
                "intent_id": intent.id,
                "partial": bool(current_intent.failed),
                "results": results,
                "materialize": {
                    "new_file_sha256": projection_receipt["new_file_sha256"],
                    "document_version_id": projection_receipt["document_version_id"],
                },
                "routing_deliveries": _routing_deliveries(current_intent),
                "post_commit_events": event_records,
            }
            full_receipt = dict(projection_receipt)
            full_receipt.update(additions)
            conn.execute(
                "UPDATE cowork_sitting_intents SET state = 'committed', new_snapshot_sha256 = ?, new_structured_head_sha256 = ?, rendered_sha256 = ?, materialization_intent_id = ?, updated_at = ?, committed_at = ?, receipt_json = ?, recovery_detail = NULL WHERE id = ? AND state = 'prepared'",
                (
                    projection_receipt["snapshot_sha256"],
                    projection_receipt["structured_head_sha256"],
                    rendered_sha,
                    projection_receipt["materialization_intent_id"],
                    projection_receipt["materialized_at"],
                    projection_receipt["materialized_at"],
                    canonical_json(full_receipt),
                    intent.id,
                ),
            )
            return additions

        resolving_flags = {
            str(entry["item"]["proposal_id"])
            for entry in intent.admitted
            if entry["item"]["verb"] in {"dismiss", "reject_plain", "reject_as_false", "reject_as_preference"}
        }
        try:
            if documents.source_is_detached(document):
                receipt = materialization.commit_managed_projection(
                    store,
                    document_id=document_id,
                    rendered_markdown=rendered_markdown,
                    rendered_sha256=rendered_sha,
                    expected_structured_head_sha256=(
                        intent.expected_structured_head_sha256
                    ),
                    snapshot_sha256=intent.expected_snapshot_sha256,
                    actor=actor,
                    replacement_snapshot=snapshot,
                    replacement_snapshot_sha256=new_snapshot_sha,
                    version_kind="materialized",
                    version_detail=f"managed_projection:sitting:{intent.id}",
                    commit_callback=_commit_callback,
                    lock_preflight=_lock_preflight,
                    resolving_flag_proposal_ids=resolving_flags,
                )
            else:
                receipt = materialization.publish_projection(
                    store,
                    document_id=document_id,
                    rendered_markdown=rendered_markdown,
                    rendered_sha256=rendered_sha,
                    expected_file_sha256=intent.expected_file_sha256,
                    expected_structured_head_sha256=(
                        intent.expected_structured_head_sha256
                    ),
                    snapshot_sha256=intent.expected_snapshot_sha256,
                    actor=actor,
                    replacement_snapshot=snapshot,
                    replacement_snapshot_sha256=new_snapshot_sha,
                    version_kind="materialized",
                    version_detail=f"sitting:{intent.id}",
                    commit_callback=_commit_callback,
                    lock_preflight=_lock_preflight,
                    resolving_flag_proposal_ids=resolving_flags,
                )
        except materialization.MaterializationError as exc:
            raise SittingError(exc.code, str(exc), status=exc.status, details=exc.details, retryable=exc.retryable) from exc
        return receipt, _receipt_events(receipt)

    if any(value is not None for value in (snapshot, snapshot_sha256, rendered_markdown, rendered_sha256)):
        raise SittingError("unexpected_commit_payload", "routing-only sittings must omit snapshot and Markdown")
    initial = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial.path),
    ):
        current_intent = _load_intent(store, intent.id, actor_ref)
        if current_intent.state == "committed" and current_intent.receipt is not None:
            ydoc_store.recover_compaction_locked(store, document_id=document_id)
            return current_intent.receipt, _receipt_events(current_intent.receipt)
        if current_intent.state != "prepared":
            raise SittingError(
                "intent_not_committable",
                f"sitting intent is {current_intent.state}",
                status=409,
            )
        if _expired(current_intent.expires_at):
            raise SittingError(
                "intent_expired",
                "sitting intent expired; prepare again",
                status=409,
            )
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise SittingError(
                "document_retired",
                "Retired documents cannot accept review decisions.",
                status=409,
            )
        if not document_surface_allowed(store, document):
            raise SittingError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        state = inspect_lifecycle_state(store, document)
        expected_projection = (
            document.content_sha256
            if documents.source_is_detached(document)
            else state.current_file_sha256
        )
        if expected_projection != intent.expected_file_sha256:
            raise SittingError(
                "stale_file",
                (
                    "Co-work projection changed before sitting commit"
                    if documents.source_is_detached(document)
                    else "Markdown file changed before sitting commit"
                ),
                status=409,
            )
        if state.structured_head_sha256 != intent.expected_structured_head_sha256 or document.ydoc_snapshot_sha256 != intent.expected_snapshot_sha256:
            raise SittingError("stale_structured_head", "structured document changed before sitting commit", status=409)
        at = _now()
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            locked_intent = _load_intent(store, intent.id, actor_ref, conn=conn)
            if locked_intent.state != "prepared":
                raise SittingError("intent_not_committable", "sitting intent changed before commit", status=409)
            results, events = _commit_decisions(store, document, actor, locked_intent, conn, at=at)
            event_records = _event_records(intent.id, events)
            receipt = {
                "ok": True,
                "intent_id": intent.id,
                "partial": bool(locked_intent.failed),
                "results": results,
                "materialize": None,
                "structured_head_sha256": intent.expected_structured_head_sha256,
                "snapshot_sha256": intent.expected_snapshot_sha256,
                "committed_at": at,
                "routing_deliveries": _routing_deliveries(locked_intent),
                "post_commit_events": event_records,
            }
            conn.execute(
                "UPDATE cowork_sitting_intents SET state = 'committed', updated_at = ?, committed_at = ?, receipt_json = ?, recovery_detail = NULL WHERE id = ? AND state = 'prepared'",
                (at, at, canonical_json(receipt), intent.id),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        store._run_on_commit()
        return receipt, event_records


def cancel_sitting(
    store: TruthStore,
    *,
    document_id: str,
    intent_id: str,
    actor: Actor,
) -> dict[str, Any]:
    actor_ref = _actor_ref(actor)
    intent = _load_intent(store, intent_id, actor_ref)
    if intent.document_id != document_id:
        raise SittingError(
            "intent_document_mismatch",
            "sitting intent belongs to another document",
            status=409,
        )
    if intent.state == "cancelled":
        return {"ok": True, "intent_id": intent.id, "state": "cancelled"}
    if intent.state == "committed":
        raise SittingError(
            "intent_already_committed",
            "committed review decisions cannot be cancelled",
            status=409,
        )
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_sitting_intents SET state = 'cancelled', updated_at = ? WHERE id = ? AND state = 'prepared'",
            (_now(), intent.id),
        )
    return {"ok": True, "intent_id": intent.id, "state": "cancelled"}


__all__ = [
    "SittingError",
    "SittingIntent",
    "cancel_sitting",
    "commit_sitting",
    "prepare_sitting",
]
