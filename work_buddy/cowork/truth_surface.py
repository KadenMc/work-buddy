"""Truth observability and guarded mutation service for Co-work documents.

The Truth kernel remains authoritative.  This module composes its lower-level
claim, evidence, lifecycle, and expression APIs into document-aware operations
that an HTTP surface can expose without teaching React about ledger tables.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping

from work_buddy.cowork import readiness
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.proposal_applicability import load_current_projection
from work_buddy.truth import documents, expressions, queries
from work_buddy.truth.anchors import CompositeSelector, reanchor, serialize_selector
from work_buddy.truth.contracts import Actor, InvariantViolation, TERMINAL_STATUSES
from work_buddy.truth.identity import parse_truth_uri, utc_now
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.redact import REDACTION_REASONS, TruthRedactor
from work_buddy.truth.review import compose_claim_review
from work_buddy.truth.store import ClaimRecord, DocumentRecord, TruthStore


_LIST_FILTERS = frozenset(
    {"all", "facts", "proposed", "challenged", "needs-review", "unconnected"}
)
_LIST_VIEWS = frozenset({"document", "folder"})


class TruthSurfaceError(InvariantViolation):
    """A typed, user-actionable failure at the Co-work Truth boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ConnectionWrite:
    claim: ClaimRecord
    claim_created: bool
    span_id: str
    expression_id: str
    expression_created: bool
    projection_sha256: str
    structured_head_sha256: str
    ydoc_generation_sha256: str


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _json_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _local_claim_id(store: TruthStore, kind: str, reference: str) -> str | None:
    if kind == "local":
        return reference
    try:
        parsed = parse_truth_uri(reference)
    except ValueError:
        return None
    if parsed.store_id != store.store_id or parsed.kind != "claim":
        return None
    return parsed.record_id


def _selector_wire(selector_json: str) -> dict[str, Any]:
    selector = CompositeSelector.from_json(selector_json)
    return {
        "exact": selector.exact,
        "prefix": selector.prefix,
        "suffix": selector.suffix,
        "start": selector.start,
        "end": selector.end,
    }


def _connections(
    store: TruthStore,
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT e.*, s.document_id, s.selector_json, s.quote_exact, "
        "d.title AS document_title, d.path AS document_path "
        "FROM expressions AS e "
        "JOIN document_spans AS s ON s.id = e.document_span_id "
        "JOIN documents AS d ON d.id = s.document_id "
        "ORDER BY d.created_at, s.created_at, e.created_at, e.id"
    ).fetchall()
    by_claim: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        claim_id = _local_claim_id(
            store,
            str(row["claim_ref_kind"]),
            str(row["claim_ref"]),
        )
        if claim_id is None:
            continue
        by_claim.setdefault(claim_id, []).append(
            {
                "expression_id": row["id"],
                "span_id": row["document_span_id"],
                "document_id": row["document_id"],
                "document_title": row["document_title"] or "",
                "document_path": row["document_path"],
                "role": row["role"],
                "quote": row["quote_exact"] or "",
                "selector": _selector_wire(row["selector_json"]),
                "claim_canonical_sha256": row["claim_canonical_sha256"],
                "created_at": row["created_at"],
                "created_by": {
                    "kind": row["created_by_kind"],
                    "ref": row["created_by_ref"],
                },
            }
        )
    return by_claim


def _available_actions(
    state: queries.ClaimState,
    *,
    can_modify: bool,
    can_decide: bool,
) -> list[str]:
    if state.claim.redacted_at is not None:
        return []
    base = state.base_status
    actions: list[str] = []
    if can_decide and base in {"proposed", "challenged"}:
        actions.append("confirm")
    elif can_decide and base == "confirmed" and state.needs_review:
        actions.append("reaffirm")
    if can_decide and base == "proposed":
        actions.append("reject")
    if can_modify and base in {"confirmed", "challenged"}:
        actions.append("challenge")
    if can_decide:
        actions.append("redact")
    return actions


def _claim_summary(
    state: queries.ClaimState,
    *,
    facts: frozenset[str],
    connections: list[dict[str, Any]],
    document_id: str,
    receipt_count: int,
    can_modify: bool,
    can_decide: bool,
) -> dict[str, Any]:
    current_connections = [
        item for item in connections if item["document_id"] == document_id
    ]
    claim = state.claim
    return {
        "claim_id": claim.id,
        "proposition": None if claim.redacted_at is not None else claim.proposition,
        "redacted": claim.redacted_at is not None,
        "claim_kind": claim.claim_kind,
        "scope": claim.scope,
        "canonical_sha256": claim.canonical_sha256,
        "status": state.status,
        "base_status": state.base_status,
        "needs_review": state.needs_review,
        "is_fact": claim.id in facts,
        "health": state.health,
        "health_reason": state.health_reason,
        "voided": state.voided,
        "valid_from": claim.valid_from,
        "valid_to": claim.valid_to,
        "effective_valid_from": state.effective_valid_from,
        "effective_valid_to": state.effective_valid_to,
        "created_at": claim.created_at,
        "created_by": {
            "kind": claim.created_by_kind,
            "ref": claim.created_by_ref,
        },
        "receipt_count": receipt_count,
        "connection_count": len(connections),
        "connected_to_document": bool(current_connections),
        "document_connections": current_connections,
        "available_actions": _available_actions(
            state,
            can_modify=can_modify,
            can_decide=can_decide,
        ),
    }


def _receipt_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT l.from_claim_id, COUNT(*) AS count "
        "FROM claim_links AS l "
        "LEFT JOIN link_retractions AS r ON r.link_id = l.id "
        "WHERE l.link_type = 'supports_span' "
        "AND l.to_kind = 'evidence_span' AND r.link_id IS NULL "
        "GROUP BY l.from_claim_id"
    ).fetchall()
    return {str(row["from_claim_id"]): int(row["count"]) for row in rows}


def truth_list(
    store: TruthStore,
    document: DocumentRecord,
    *,
    view: str = "document",
    filter_name: str = "all",
    offset: int = 0,
    limit: int = 100,
    read_only: bool = False,
) -> dict[str, Any]:
    """Project a compact, paginated Truth list for one editor context."""

    normalized_view = str(view).strip().lower()
    normalized_filter = str(filter_name).strip().lower().replace("_", "-")
    if normalized_view not in _LIST_VIEWS:
        raise TruthSurfaceError("invalid_view", "view must be document or folder")
    if normalized_filter not in _LIST_FILTERS:
        raise TruthSurfaceError(
            "invalid_filter",
            "filter must be all, facts, proposed, challenged, needs-review, or unconnected",
        )
    if offset < 0 or limit < 1 or limit > 200:
        raise TruthSurfaceError(
            "invalid_pagination", "offset must be nonnegative and limit must be 1 through 200"
        )
    if not document_surface_allowed(store, document):
        raise TruthSurfaceError(
            "policy_forbidden",
            "This document is not available in Co-work for this folder.",
            status=403,
        )

    with store._read_connection() as conn:
        conn.execute("BEGIN")
        try:
            states = queries.resolve_claim_states(store, conn=conn)
            fact_ids = frozenset(
                item.claim_id
                for item in queries.current_claims(
                    store,
                    valid_at=utc_now(),
                    conn=conn,
                )
            )
            by_claim = _connections(store, conn)
            receipt_counts = _receipt_counts(conn)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")

    document_active = documents.current_lifecycle(store, document.id) == "active"
    can_modify = not read_only and document_active
    can_decide = (
        can_modify and "dashboard" in store.profile.gate.confirmation_surfaces
    )
    summaries = [
        _claim_summary(
            state,
            facts=fact_ids,
            connections=by_claim.get(state.claim_id, []),
            document_id=document.id,
            receipt_count=receipt_counts.get(state.claim_id, 0),
            can_modify=can_modify,
            can_decide=can_decide,
        )
        for state in states
    ]
    if normalized_view == "document":
        summaries = [item for item in summaries if item["connected_to_document"]]

    def matches(item: dict[str, Any]) -> bool:
        if normalized_filter == "all":
            return True
        if normalized_filter == "facts":
            return bool(item["is_fact"])
        if normalized_filter == "needs-review":
            return bool(item["needs_review"])
        if normalized_filter == "unconnected":
            return item["connection_count"] == 0
        return item["base_status"] == normalized_filter

    filtered = [item for item in summaries if matches(item)]
    filtered.sort(
        key=lambda item: (
            0 if item["connected_to_document"] else 1,
            min(
                (
                    connection["selector"]["start"]
                    for connection in item["document_connections"]
                    if connection["selector"]["start"] is not None
                ),
                default=2**63 - 1,
            ),
            item["created_at"],
            item["claim_id"],
        )
    )
    page = filtered[offset : offset + limit]
    counts = {
        "all": len(summaries),
        "facts": sum(bool(item["is_fact"]) for item in summaries),
        "proposed": sum(item["base_status"] == "proposed" for item in summaries),
        "challenged": sum(item["base_status"] == "challenged" for item in summaries),
        "needs_review": sum(bool(item["needs_review"]) for item in summaries),
        "unconnected": sum(item["connection_count"] == 0 for item in summaries),
    }
    return {
        "schema": "cowork-truth/v1",
        "store_id": store.store_id,
        "document_id": document.id,
        "view": normalized_view,
        "filter": normalized_filter,
        "counts": counts,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + len(page) if offset + len(page) < len(filtered) else None,
        "claims": page,
        "read_only": read_only,
        "capabilities": {
            "can_observe": True,
            "can_modify": can_modify,
            "can_decide": can_decide,
            "allowed_claim_kinds": list(store.profile.allowed_claim_kinds),
            "mutation_unavailable_reason": (
                "Co-work is read-only right now."
                if read_only
                else (
                    "This document is retired, so its Truth connections cannot change."
                    if not document_active
                    else None
                )
            ),
        },
    }


def _claim_state(
    store: TruthStore,
    claim_id: str,
    *,
    conn: sqlite3.Connection,
) -> queries.ClaimState:
    for state in queries.resolve_claim_states(store, conn=conn):
        if state.claim_id == claim_id:
            return state
    raise TruthSurfaceError("claim_not_found", "claim does not exist", status=404)


def _receipts(conn: sqlite3.Connection, claim_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT l.id AS link_id, l.created_at AS linked_at, "
        "s.id AS span_id, s.selector_json, s.quote_exact, s.span_sha256, "
        "s.author_kind, s.author_ref, s.redacted_at AS span_redacted_at, "
        "e.id AS evidence_id, e.kind AS evidence_kind, e.source_locator, "
        "e.content_sha256, e.media_type, e.trust_class, e.derived_from_store, "
        "e.acquired_at, e.acquisition_method, e.redacted_at AS evidence_redacted_at "
        "FROM claim_links AS l "
        "JOIN evidence_spans AS s ON s.id = l.to_ref "
        "JOIN evidence AS e ON e.id = s.evidence_id "
        "LEFT JOIN link_retractions AS r ON r.link_id = l.id "
        "WHERE l.from_claim_id = ? AND l.link_type = 'supports_span' "
        "AND l.to_kind = 'evidence_span' AND r.link_id IS NULL "
        "ORDER BY l.created_at, l.id",
        (claim_id,),
    ).fetchall()
    return [
        {
            "link_id": row["link_id"],
            "linked_at": row["linked_at"],
            "span_id": row["span_id"],
            "selector": _selector_wire(row["selector_json"]),
            "quote": row["quote_exact"],
            "span_sha256": row["span_sha256"],
            "author": {"kind": row["author_kind"], "ref": row["author_ref"]},
            "span_redacted_at": row["span_redacted_at"],
            "evidence_id": row["evidence_id"],
            "evidence_kind": row["evidence_kind"],
            "source_locator": row["source_locator"],
            "content_sha256": row["content_sha256"],
            "media_type": row["media_type"],
            "trust_class": row["trust_class"],
            "derived_from_store": row["derived_from_store"],
            "acquired_at": row["acquired_at"],
            "acquisition_method": row["acquisition_method"],
            "evidence_redacted_at": row["evidence_redacted_at"],
        }
        for row in rows
    ]


def _claim_observability_context(
    store: TruthStore,
    claim_id: str,
    *,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Read the complete decision-relevant claim context from one snapshot.

    Co-work shows more than evidence receipts when a human makes a guarded
    Truth decision.  Keep that entire visible context in one canonical object
    so a lifecycle, relation, integrity, premise, or expression change makes a
    previously displayed decision binding stale.
    """

    state = _claim_state(store, claim_id, conn=conn)
    is_fact = any(
        item.claim_id == claim_id
        for item in queries.current_claims(
            store,
            valid_at=utc_now(),
            conn=conn,
        )
    )
    receipts = _receipts(conn, claim_id)
    support = _json_value(TruthLifecycle(store).assess_support(claim_id, conn=conn))
    premises = _json_value(TruthLifecycle(store).assess_premises(claim_id, conn=conn))
    history = [
        dict(row)
        for row in conn.execute(
            "SELECT id, seq, status, at, actor_kind, actor_ref, "
            "basis_kind, basis_ref, note FROM claim_status_events "
            "WHERE claim_id = ? ORDER BY seq",
            (claim_id,),
        ).fetchall()
    ]
    derivations: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM derivations WHERE claim_id = ? ORDER BY created_at, id",
        (claim_id,),
    ).fetchall():
        value = dict(row)
        value["premises"] = [
            dict(item)
            for item in conn.execute(
                "SELECT premise_kind, premise_ref FROM derivation_premises "
                "WHERE derivation_id = ? ORDER BY premise_kind, premise_ref",
                (row["id"],),
            ).fetchall()
        ]
        derivations.append(value)
    links = [
        {
            **dict(row),
            "role": _json_object(row["role_json"]),
            "retracted": row["retracted_at"] is not None,
        }
        for row in conn.execute(
            "SELECT l.*, r.at AS retracted_at, r.reason AS retraction_reason "
            "FROM claim_links AS l "
            "LEFT JOIN link_retractions AS r ON r.link_id = l.id "
            "WHERE l.from_claim_id = ? OR (l.to_kind = 'claim' AND l.to_ref = ?) "
            "ORDER BY l.created_at, l.id",
            (claim_id, claim_id),
        ).fetchall()
    ]
    conflicts: list[dict[str, Any]] = []
    for item in queries.conflicts(store, claim_id=claim_id, conn=conn):
        other_claim_id = (
            item.to_claim_id
            if item.from_claim_id == claim_id
            else item.from_claim_id
        )
        other_claim = store.get_claim(other_claim_id, conn=conn)
        other_status = (
            item.to_status
            if item.to_claim_id == other_claim_id
            else item.from_status
        )
        conflicts.append(
            {
                "relation_id": item.link_id,
                "claim_id": other_claim_id,
                "proposition": (
                    None
                    if other_claim is None or other_claim.redacted_at is not None
                    else other_claim.proposition
                ),
                "status": other_status,
                "conflict_type": item.conflict_type,
                "conflict_class": item.conflict_class,
                "direction": (
                    "challenges"
                    if item.from_claim_id == claim_id
                    else "challenged_by"
                ),
                "created_at": item.created_at,
            }
        )
    receipt_evidence_ids = {receipt["evidence_id"] for receipt in receipts}
    integrity_by_evidence = {
        item.evidence_id: _json_value(item)
        for item in queries.source_integrity_states(store, conn=conn)
        if item.evidence_id in receipt_evidence_ids
    }
    connections = _connections(store, conn).get(claim_id, [])
    decision_context = {
        "claim_state": {
            "base_status": state.base_status,
            "status": state.status,
            "needs_review": state.needs_review,
            "voided": state.voided,
            "health": state.health,
            "health_reason": state.health_reason,
            "effective_valid_from": state.effective_valid_from,
            "effective_valid_to": state.effective_valid_to,
            "redacted_at": state.claim.redacted_at,
            "is_fact": is_fact,
        },
        "status_history": history,
        "receipt_details": receipts,
        "support": support,
        "premises": premises,
        "derivations": derivations,
        "links": links,
        "conflicts": conflicts,
        "connections": connections,
        "source_integrity": integrity_by_evidence,
    }
    return {
        "state": state,
        "is_fact": is_fact,
        "receipts": receipts,
        "support": support,
        "premises": premises,
        "history": history,
        "derivations": derivations,
        "links": links,
        "conflicts": conflicts,
        "connections": connections,
        "integrity_by_evidence": integrity_by_evidence,
        "decision_context": decision_context,
    }


def truth_claim_detail(
    store: TruthStore,
    document: DocumentRecord,
    claim_id: str,
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    """Return one complete, observational claim inspector payload."""

    if not document_surface_allowed(store, document):
        raise TruthSurfaceError(
            "policy_forbidden",
            "This document is not available in Co-work for this folder.",
            status=403,
        )
    with store._read_connection() as conn:
        conn.execute("BEGIN")
        try:
            context = _claim_observability_context(store, claim_id, conn=conn)
            state = context["state"]
            facts = frozenset({claim_id}) if context["is_fact"] else frozenset()
            connections = context["connections"]
            receipts = context["receipts"]
            document_active = (
                documents.current_lifecycle(store, document.id) == "active"
            )
            can_modify = not read_only and document_active
            can_decide = (
                can_modify
                and "dashboard" in store.profile.gate.confirmation_surfaces
            )
            summary = _claim_summary(
                state,
                facts=facts,
                connections=connections,
                document_id=document.id,
                receipt_count=len(receipts),
                can_modify=can_modify,
                can_decide=can_decide,
            )
            if bool(context["support"].get("quarantined_only")):
                summary["available_actions"] = [
                    action
                    for action in summary["available_actions"]
                    if action not in {"confirm", "reaffirm"}
                ]
            decision_binding = None
            if state.claim.redacted_at is None and can_decide:
                review = compose_claim_review(
                    store,
                    claim_id,
                    action="confirm",
                    additional_context=context["decision_context"],
                    conn=conn,
                )
                decision_binding = {
                    "payload_sha256": review.payload_sha256,
                    "context_sha256": review.context_sha256,
                    "agent_authored_only": review.agent_authored_only,
                }
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    for receipt in receipts:
        receipt["integrity"] = context["integrity_by_evidence"].get(
            receipt["evidence_id"]
        )
    return {
        "schema": "cowork-truth-claim/v1",
        "store_id": store.store_id,
        "document_id": document.id,
        "claim": {
            **summary,
            "structured": _json_object(state.claim.structured_json),
            "confidence_extraction": state.claim.confidence_extraction,
            "meta": _json_object(state.claim.meta_json),
        },
        "status_history": context["history"],
        "receipts": receipts,
        "support": context["support"],
        "premises": context["premises"],
        "derivations": context["derivations"],
        "links": context["links"],
        "conflicts": context["conflicts"],
        "connections": connections,
        "decision_binding": decision_binding,
    }


def _selector_from_input(value: Any) -> CompositeSelector:
    if not isinstance(value, Mapping):
        raise TruthSurfaceError("invalid_selector", "selector must be an object")
    try:
        return CompositeSelector(
            exact=value.get("exact"),
            prefix=value.get("prefix", ""),
            suffix=value.get("suffix", ""),
            start=value.get("start"),
            end=value.get("end"),
        )
    except InvariantViolation as exc:
        raise TruthSurfaceError("invalid_selector", str(exc)) from exc
    except Exception as exc:
        raise TruthSurfaceError("invalid_selector", str(exc)) from exc


def _current_connection_target(
    store: TruthStore,
    document: DocumentRecord,
    *,
    selector: CompositeSelector,
    expected_structured_head_sha256: str,
    expected_ydoc_generation_sha256: str | None,
    expected_projection_sha256: str,
) -> tuple[CompositeSelector, Any]:
    state = readiness.classify_document(store, document, read_only=False)
    if state.initialization_state != "ready" or state.structured_head_sha256 is None:
        raise TruthSurfaceError(
            "document_not_ready",
            "The document must finish loading before Truth can connect its prose.",
            status=409,
            retryable=True,
        )
    if state.structured_head_sha256 != expected_structured_head_sha256:
        raise TruthSurfaceError(
            "stale_document",
            "The document changed before the Truth connection was saved.",
            status=409,
            retryable=True,
            details={"structured_head_sha256": state.structured_head_sha256},
        )
    generation = documents.current_ydoc_generation(store, document.id)
    if (
        expected_ydoc_generation_sha256 is not None
        and generation != expected_ydoc_generation_sha256
    ):
        raise TruthSurfaceError(
            "stale_document",
            "The document changed before the Truth connection was saved.",
            status=409,
            retryable=True,
            details={"ydoc_generation_sha256": generation},
        )
    projection, reason = load_current_projection(
        store,
        document,
        structured_head_sha256=state.structured_head_sha256,
    )
    if projection is None:
        raise TruthSurfaceError(
            "projection_unavailable",
            "Save the current editor state before connecting it to Truth.",
            status=409,
            retryable=True,
            details={"reason": reason},
        )
    if projection.projection_sha256 != expected_projection_sha256:
        raise TruthSurfaceError(
            "stale_document",
            "The document changed before the Truth connection was saved.",
            status=409,
            retryable=True,
            details={"projection_sha256": projection.projection_sha256},
        )
    try:
        resolved = reanchor(
            projection.text,
            selector,
            expected_snapshot_sha256=expected_projection_sha256,
        )
    except InvariantViolation as exc:
        raise TruthSurfaceError(
            "selection_not_verifiable",
            "The selected passage is no longer uniquely present in the document.",
            status=409,
            retryable=True,
            details={"reason": str(exc)},
        ) from exc
    return (
        CompositeSelector(
            exact=resolved.exact,
            prefix=selector.prefix,
            suffix=selector.suffix,
            start=resolved.start,
            end=resolved.end,
        ),
        projection,
    )


def connect_claim(
    store: TruthStore,
    document: DocumentRecord,
    *,
    actor: Actor,
    selector_input: Any,
    role: str,
    expected_structured_head_sha256: str,
    expected_projection_sha256: str,
    expected_ydoc_generation_sha256: str | None = None,
    claim_id: str | None = None,
    claim_input: Mapping[str, Any] | None = None,
) -> ConnectionWrite:
    """Atomically connect selected prose to an existing or newly proposed claim."""

    if actor.kind != "human" or not actor.ref:
        raise TruthSurfaceError("human_actor_required", "a dashboard human actor is required")
    if not document_surface_allowed(store, document):
        raise TruthSurfaceError(
            "policy_forbidden",
            "This document is not available in Co-work for this folder.",
            status=403,
        )
    if documents.current_lifecycle(store, document.id) != "active":
        raise TruthSurfaceError(
            "document_retired", "A retired document cannot gain Truth connections.", status=409
        )
    if (claim_id is None) == (claim_input is None):
        raise TruthSurfaceError(
            "invalid_claim_target",
            "Supply either claim_id or a new claim, but not both.",
        )
    normalized_role = str(role or "").strip()
    if normalized_role not in expressions.EXPRESSION_ROLES:
        raise TruthSurfaceError(
            "invalid_role",
            f"role must be one of {sorted(expressions.EXPRESSION_ROLES)}",
        )
    selector = _selector_from_input(selector_input)
    resolved_selector, projection = _current_connection_target(
        store,
        document,
        selector=selector,
        expected_structured_head_sha256=expected_structured_head_sha256,
        expected_ydoc_generation_sha256=expected_ydoc_generation_sha256,
        expected_projection_sha256=expected_projection_sha256,
    )
    selector_json = serialize_selector(resolved_selector)

    with store.write_transaction() as conn:
        if claim_input is not None:
            proposition = claim_input.get("proposition")
            claim_kind = claim_input.get("claim_kind")
            written = store.propose_claim(
                proposition=proposition,
                claim_kind=claim_kind,
                actor=actor,
                structured=claim_input.get("structured"),
                scope=str(claim_input.get("scope") or "store"),
                valid_from=claim_input.get("valid_from"),
                valid_to=claim_input.get("valid_to"),
                confidence_extraction=claim_input.get("confidence_extraction"),
                meta={"surface": "cowork_truth"},
                conn=conn,
            )
            claim = written.claim
            claim_created = written.created
        else:
            claim = store.get_claim(str(claim_id or ""), conn=conn)
            if claim is None:
                raise TruthSurfaceError("claim_not_found", "claim does not exist", status=404)
            claim_created = False

        claim_state = _claim_state(store, claim.id, conn=conn)
        if (
            claim.redacted_at is not None
            or claim_state.voided
            or claim_state.base_status in TERMINAL_STATUSES
        ):
            raise TruthSurfaceError(
                "claim_not_connectable",
                "This claim is no longer active enough to connect to document text.",
                status=409,
                retryable=True,
            )

        existing = conn.execute(
            "SELECT e.id AS expression_id, s.id AS span_id "
            "FROM expressions AS e "
            "JOIN document_spans AS s ON s.id = e.document_span_id "
            "WHERE s.document_id = ? AND s.selector_json = ? "
            "AND e.claim_ref_kind = 'local' AND e.claim_ref = ? AND e.role = ? "
            "ORDER BY e.created_at, e.id LIMIT 1",
            (document.id, selector_json, claim.id, normalized_role),
        ).fetchone()
        if existing is not None:
            return ConnectionWrite(
                claim=claim,
                claim_created=claim_created,
                span_id=existing["span_id"],
                expression_id=existing["expression_id"],
                expression_created=False,
                projection_sha256=projection.projection_sha256,
                structured_head_sha256=projection.structured_head_sha256,
                ydoc_generation_sha256=projection.generation_sha256,
            )

        span = expressions._ensure_document_span_locked(
            store,
            conn,
            document_id=document.id,
            selector=resolved_selector,
            quote_exact=resolved_selector.exact,
            actor=actor,
            author_kind="unknown",
            author_ref=None,
            reuse_existing=False,
        )
        expression = expressions._mark_expression_locked(
            store,
            conn,
            document_span_id=span.id,
            claim_ref=claim.id,
            role=normalized_role,
            actor=actor,
            meta={
                "base_content_sha256": projection.projection_sha256,
                "base_structured_head_sha256": projection.structured_head_sha256,
                "base_ydoc_generation_sha256": projection.generation_sha256,
                "projection_binding_id": projection.binding_id,
            },
        )
    return ConnectionWrite(
        claim=claim,
        claim_created=claim_created,
        span_id=span.id,
        expression_id=expression.id,
        expression_created=True,
        projection_sha256=projection.projection_sha256,
        structured_head_sha256=projection.structured_head_sha256,
        ydoc_generation_sha256=projection.generation_sha256,
    )


def _require_decision_binding(
    review: Any,
    *,
    expected_canonical_sha256: str,
    expected_context_sha256: str,
) -> None:
    if review.payload_sha256 != str(expected_canonical_sha256 or "").strip().lower():
        raise TruthSurfaceError(
            "stale_claim",
            "The claim changed after it was shown. Review it again.",
            status=409,
            retryable=True,
        )
    if review.context_sha256 != str(expected_context_sha256 or "").strip().lower():
        raise TruthSurfaceError(
            "stale_truth_context",
            "The claim's Truth context changed after it was shown. Review it again.",
            status=409,
            retryable=True,
        )


def _require_decision_action(
    state: queries.ClaimState,
    *,
    action: str,
    gesture_kind: str | None,
    support: Mapping[str, Any],
) -> str | None:
    """Validate the named ceremony against the exact state in this transaction."""

    if action == "confirm":
        if state.base_status not in {"proposed", "challenged"}:
            raise TruthSurfaceError(
                "invalid_decision_state",
                "Confirm is only available for a proposed or challenged claim.",
                status=409,
                retryable=True,
            )
        expected_gesture = "confirm"
    elif action == "reaffirm":
        if state.base_status != "confirmed" or not state.needs_review:
            raise TruthSurfaceError(
                "invalid_decision_state",
                "Reaffirm is only available for a confirmed claim that needs review.",
                status=409,
                retryable=True,
            )
        expected_gesture = "reaffirm"
    elif action == "reject":
        if state.base_status != "proposed":
            raise TruthSurfaceError(
                "invalid_decision_state",
                "Reject is only available for a proposed claim.",
                status=409,
                retryable=True,
            )
        if gesture_kind is not None:
            raise TruthSurfaceError(
                "invalid_gesture_kind",
                "reject does not accept gesture_kind",
            )
        return None
    elif action == "redact":
        if state.claim.redacted_at is not None:
            raise TruthSurfaceError(
                "invalid_decision_state",
                "This claim has already been redacted.",
                status=409,
                retryable=True,
            )
        if gesture_kind is not None:
            raise TruthSurfaceError(
                "invalid_gesture_kind",
                "redact does not accept gesture_kind",
            )
        return None
    else:
        raise TruthSurfaceError(
            "unsupported_decision",
            "action must be confirm, reaffirm, reject, or redact",
        )

    normalized_gesture = (
        expected_gesture
        if gesture_kind is None
        else str(gesture_kind).strip().lower().replace("-", "_")
    )
    if normalized_gesture != expected_gesture:
        raise TruthSurfaceError(
            "invalid_gesture_kind",
            f"{action} requires gesture_kind {expected_gesture!r}",
        )
    if bool(support.get("quarantined_only")):
        raise TruthSurfaceError(
            "quarantined_confirmation_unavailable",
            "This surface cannot confirm a claim supported only by quarantined evidence.",
            status=409,
        )
    return expected_gesture


def decide_claim(
    store: TruthStore,
    document: DocumentRecord,
    claim_id: str,
    *,
    actor: Actor,
    action: str,
    expected_canonical_sha256: str,
    expected_context_sha256: str,
    gesture_kind: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply one exact confirm, plain reject, or redaction decision."""

    if actor.kind != "human" or not actor.ref:
        raise TruthSurfaceError("human_actor_required", "a dashboard human actor is required")
    if not document_surface_allowed(store, document):
        raise TruthSurfaceError(
            "policy_forbidden",
            "This document is not available in Co-work for this folder.",
            status=403,
        )
    if documents.current_lifecycle(store, document.id) != "active":
        raise TruthSurfaceError(
            "document_retired", "A retired document cannot change Truth.", status=409
        )
    if "dashboard" not in store.profile.gate.confirmation_surfaces:
        raise TruthSurfaceError(
            "policy_forbidden",
            "This folder does not allow Truth decisions from the dashboard.",
            status=403,
        )
    normalized = str(action or "").strip().lower().replace("_", "-")
    lifecycle = TruthLifecycle(store)
    with store.write_transaction() as conn:
        context = _claim_observability_context(
            store,
            claim_id,
            conn=conn,
        )
        decision_context = context["decision_context"]
        validated_gesture_kind = _require_decision_action(
            context["state"],
            action=normalized,
            gesture_kind=gesture_kind,
            support=context["support"],
        )
        if normalized in {"confirm", "reaffirm"}:
            assert validated_gesture_kind is not None
            kind = validated_gesture_kind
            review = compose_claim_review(
                store,
                claim_id,
                action="confirm",
                decision={"gesture_kind": kind},
                additional_context=decision_context,
                conn=conn,
            )
            _require_decision_binding(
                review,
                expected_canonical_sha256=expected_canonical_sha256,
                expected_context_sha256=expected_context_sha256,
            )
            gesture = lifecycle.mint_gesture(
                subject_ref=claim_id,
                actor=actor,
                surface="dashboard",
                kind=kind,
                displayed_payload_sha256=review.payload_sha256,
                context_sha256=review.context_sha256,
                conn=conn,
            )
            result = lifecycle.confirm_claim(
                claim_id=claim_id,
                gesture_id=gesture.id,
                actor=actor,
                expected_context_sha256=review.context_sha256,
                conn=conn,
            )
        elif normalized == "reject":
            review = compose_claim_review(
                store,
                claim_id,
                action="reject",
                decision={"reason_class": "reject_plain"},
                additional_context=decision_context,
                conn=conn,
            )
            _require_decision_binding(
                review,
                expected_canonical_sha256=expected_canonical_sha256,
                expected_context_sha256=expected_context_sha256,
            )
            gesture = lifecycle.mint_gesture(
                subject_ref=claim_id,
                actor=actor,
                surface="dashboard",
                kind="reject_plain",
                displayed_payload_sha256=review.payload_sha256,
                context_sha256=review.context_sha256,
                conn=conn,
            )
            result = lifecycle.reject_claim(
                source_claim_id=claim_id,
                gesture_id=gesture.id,
                actor=actor,
                reason_class="reject_plain",
                expected_context_sha256=review.context_sha256,
                conn=conn,
            )
        elif normalized == "redact":
            redaction_reason = str(reason or "privacy").strip().lower()
            if redaction_reason not in REDACTION_REASONS:
                raise TruthSurfaceError(
                    "invalid_redaction_reason",
                    f"reason must be one of {sorted(REDACTION_REASONS)}",
                )
            review = compose_claim_review(
                store,
                claim_id,
                action="redact",
                decision={"reason": redaction_reason, "subject_kind": "claim"},
                additional_context=decision_context,
                conn=conn,
            )
            _require_decision_binding(
                review,
                expected_canonical_sha256=expected_canonical_sha256,
                expected_context_sha256=expected_context_sha256,
            )
            gesture = lifecycle.mint_gesture(
                subject_ref=claim_id,
                actor=actor,
                surface="dashboard",
                kind="redact",
                displayed_payload_sha256=review.payload_sha256,
                context_sha256=review.context_sha256,
                conn=conn,
            )
            result = TruthRedactor(store, lifecycle=lifecycle).redact(
                subject_kind="claim",
                subject_ref=claim_id,
                actor=actor,
                reason=redaction_reason,
                basis_kind="gesture",
                basis_ref=gesture.id,
                expected_context_sha256=review.context_sha256,
                conn=conn,
            )
        else:  # pragma: no cover - validated by _require_decision_action
            raise AssertionError(f"unhandled validated Truth decision: {normalized}")
    return {
        "action": normalized,
        "claim_id": claim_id,
        "gesture_id": gesture.id,
        "result": _json_value(result),
    }


def challenge_claim(
    store: TruthStore,
    document: DocumentRecord,
    claim_id: str,
    *,
    actor: Actor,
    challenging_claim_id: str,
    expected_canonical_sha256: str,
    expected_challenger_sha256: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Challenge one exact claim with one exact, already-supported claim."""

    if actor.kind != "human" or not actor.ref:
        raise TruthSurfaceError("human_actor_required", "a dashboard human actor is required")
    if not document_surface_allowed(store, document):
        raise TruthSurfaceError("policy_forbidden", "This document is not available in Co-work.", status=403)
    if documents.current_lifecycle(store, document.id) != "active":
        raise TruthSurfaceError(
            "document_retired",
            "A retired document cannot change Truth.",
            status=409,
        )
    with store.write_transaction() as conn:
        target = store.get_claim(claim_id, conn=conn)
        challenger = store.get_claim(challenging_claim_id, conn=conn)
        if target is None or challenger is None:
            raise TruthSurfaceError("claim_not_found", "claim does not exist", status=404)
        if target.redacted_at is not None:
            raise TruthSurfaceError(
                "claim_not_challengeable",
                "A redacted claim cannot be challenged.",
                status=409,
            )
        if target.canonical_sha256 != str(expected_canonical_sha256).strip().lower():
            raise TruthSurfaceError("stale_claim", "The claim changed after it was shown.", status=409, retryable=True)
        if challenger.canonical_sha256 != str(expected_challenger_sha256).strip().lower():
            raise TruthSurfaceError("stale_challenger", "The challenging claim changed after it was shown.", status=409, retryable=True)
        result = TruthLifecycle(store).challenge_claim(
            claim_id=claim_id,
            challenging_claim_id=challenging_claim_id,
            actor=actor,
            note=note,
            conn=conn,
        )
    return {"action": "challenge", "claim_id": claim_id, "result": _json_value(result)}


__all__ = [
    "ConnectionWrite",
    "TruthSurfaceError",
    "challenge_claim",
    "connect_claim",
    "decide_claim",
    "truth_claim_detail",
    "truth_list",
]
