"""Document spans and the expression relation (tms-glue section II.4).

An expression is the downstream span relation: "this passage SAYS this claim".
It is an immutable link row whose staleness is DERIVED from two fingerprints
captured at link time (claim-side and span-side), never by mutating the row.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from work_buddy.truth.anchors import (
    CompositeSelector,
    parse_selector,
    serialize_selector,
)
from work_buddy.truth.contracts import (
    Actor,
    InvariantViolation,
    TERMINAL_STATUSES,
)
from work_buddy.truth.identity import (
    canonical_json,
    parse_truth_uri,
    sha256_text,
)
from work_buddy.truth.store import (
    EXPRESSION_ROLES,
    SPAN_AUTHOR_KINDS,
    DocumentSpanRecord,
    ExpressionRecord,
    TruthStore,
    _record_id,
    _require_text,
    _timestamp,
    _valid_record_id,
)


@dataclass(frozen=True, slots=True)
class StaleExpression:
    """One expression whose claim-side or span-side fingerprint has drifted."""

    expression: ExpressionRecord
    claim_side_stale: bool
    span_side_stale: bool


def _serialize_selector_value(selector: Any) -> str:
    """Normalize a selector into stored Web Annotation selector_json."""
    if isinstance(selector, CompositeSelector):
        return serialize_selector(selector)
    if isinstance(selector, (bytes, bytearray, str)):
        # Round-trip through the shared parser so stored JSON stays canonical
        # and valid for the selector reader.
        return serialize_selector(parse_selector(selector))
    if isinstance(selector, (list, dict)):
        return serialize_selector(CompositeSelector.from_web_annotation(selector))
    raise InvariantViolation("selector must be a CompositeSelector, JSON, list, or dict")


def _resolve_author(
    actor: Actor,
    author_kind: str | None,
    author_ref: str | None,
) -> tuple[str | None, str | None]:
    if author_kind is None:
        derived = {
            "human": "human",
            "agent_run": "agent_run",
            "system": "unknown",
        }[actor.kind]
        resolved_ref = author_ref if derived != "unknown" else None
        return derived, resolved_ref
    if author_kind not in SPAN_AUTHOR_KINDS:
        raise InvariantViolation(
            f"author_kind must be one of {sorted(SPAN_AUTHOR_KINDS)}"
        )
    if author_kind == "unknown" and author_ref is not None:
        raise InvariantViolation("unknown span authorship cannot carry author_ref")
    return author_kind, author_ref


def _ensure_document_span_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    document_id: str,
    selector: Any,
    quote_exact: str,
    actor: Actor,
    author_kind: str | None = None,
    author_ref: str | None = None,
    at: str | None = None,
) -> DocumentSpanRecord:
    """Anchor (or reuse by span_sha256) a document span on a shared transaction."""
    document_ref = _valid_record_id(document_id, "document_id")
    quote = _require_text(quote_exact, "quote_exact")
    span_sha256 = sha256_text(quote)
    selector_json = _serialize_selector_value(selector)
    resolved_author, resolved_ref = _resolve_author(actor, author_kind, author_ref)
    created = _timestamp(at, "document span at")
    if store._get_document_locked(conn, document_ref) is None:
        raise InvariantViolation(f"document does not exist: {document_ref}")
    existing = store._find_document_span_locked(
        conn,
        document_id=document_ref,
        span_sha256=span_sha256,
    )
    if existing is not None:
        return existing
    record = DocumentSpanRecord(
        id=_record_id(None, "document span id"),
        document_id=document_ref,
        selector_json=selector_json,
        quote_exact=quote,
        span_sha256=span_sha256,
        author_kind=resolved_author,
        author_ref=resolved_ref,
        created_at=created,
        created_by_kind=actor.kind,
        created_by_ref=actor.ref,
    )
    return store._insert_document_span_locked(conn, record)


def ensure_document_span(
    store: TruthStore,
    *,
    document_id: str,
    selector: Any,
    quote_exact: str,
    actor: Actor,
    author_kind: str | None = None,
    author_ref: str | None = None,
    at: str | None = None,
) -> DocumentSpanRecord:
    """Anchor (or reuse by span_sha256) a document span for binding."""
    with store.write_transaction() as conn:
        return _ensure_document_span_locked(
            store,
            conn,
            document_id=document_id,
            selector=selector,
            quote_exact=quote_exact,
            actor=actor,
            author_kind=author_kind,
            author_ref=author_ref,
            at=at,
        )


def _classify_claim_ref(store: TruthStore, claim_ref: str) -> tuple[str, str, str]:
    """Return (claim_ref_kind, stored_ref, local_claim_id_or_empty).

    A local claim id resolves in-store. A wb-truth URI into THIS store resolves
    to its local claim id (so the mint path can capture the canonical
    fingerprint). A URI into another store is a valid stored ref but cannot be
    resolved for minting.
    """
    ref = _require_text(claim_ref, "claim_ref")
    if ref.startswith("wb-truth://"):
        parsed = parse_truth_uri(ref)
        if parsed.kind != "claim":
            raise InvariantViolation("expression claim_ref URI must reference a claim")
        local = parsed.record_id if parsed.store_id == store.store_id else ""
        return "uri", parsed.uri, local
    return "local", _valid_record_id(ref, "claim_ref"), _valid_record_id(
        ref, "claim_ref"
    )


def _mark_expression_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    document_span_id: str,
    claim_ref: str,
    role: str,
    actor: Actor,
    at: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> ExpressionRecord:
    """Mint one expression on a shared transaction, capturing both fingerprints."""
    span_ref = _valid_record_id(document_span_id, "document_span_id")
    expression_role = _require_text(role, "role")
    if expression_role not in EXPRESSION_ROLES:
        raise InvariantViolation(
            f"role must be one of {sorted(EXPRESSION_ROLES)}"
        )
    claim_ref_kind, stored_ref, local_id = _classify_claim_ref(store, claim_ref)
    created = _timestamp(at, "expression at")
    span = store._get_document_span_locked(conn, span_ref)
    if span is None:
        raise InvariantViolation(f"document span does not exist: {span_ref}")
    if not local_id:
        raise InvariantViolation(
            "expression minting requires a claim resolvable in this store"
        )
    claim = store._get_claim_locked(conn, local_id)
    if claim is None:
        raise InvariantViolation(f"claim does not exist: {local_id}")
    document = store._get_document_locked(conn, span.document_id)
    base_meta: dict[str, Any] = dict(meta or {})
    if document is not None:
        base_meta.setdefault("base_content_sha256", document.content_sha256)
    record = ExpressionRecord(
        id=_record_id(None, "expression id"),
        document_span_id=span_ref,
        claim_ref_kind=claim_ref_kind,
        claim_ref=stored_ref,
        role=expression_role,
        claim_canonical_sha256=claim.canonical_sha256,
        span_sha256=span.span_sha256,
        created_at=created,
        created_by_kind=actor.kind,
        created_by_ref=actor.ref,
        meta_json=canonical_json(base_meta) if base_meta else None,
    )
    return store._insert_expression_locked(conn, record)


def mark_expression(
    store: TruthStore,
    *,
    document_span_id: str,
    claim_ref: str,
    role: str,
    actor: Actor,
    at: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> ExpressionRecord:
    """Link a passage to a claim it expresses (propose-weight, PRD section 8).

    Captures the claim-side canonical fingerprint and the span-side span_sha256
    at link time, so later claim supersession or document edit drift stales the
    expression mechanically.
    """
    with store.write_transaction() as conn:
        return _mark_expression_locked(
            store,
            conn,
            document_span_id=document_span_id,
            claim_ref=claim_ref,
            role=role,
            actor=actor,
            at=at,
            meta=meta,
        )


def _expressions_for_document_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    document_id: str,
) -> tuple[ExpressionRecord, ...]:
    rows = conn.execute(
        "SELECT e.* FROM expressions e "
        "JOIN document_spans s ON s.id = e.document_span_id "
        "WHERE s.document_id = ? ORDER BY e.created_at, e.id",
        (document_id,),
    ).fetchall()
    return tuple(ExpressionRecord(**dict(row)) for row in rows)


def expressions_for_document(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[ExpressionRecord, ...]:
    """Read path for the click-a-sentence inspector: passages to claims."""
    identifier = _valid_record_id(document_id, "document_id")
    if conn is not None:
        return _expressions_for_document_locked(store, conn, identifier)
    with store._read_connection() as read_conn:
        return _expressions_for_document_locked(store, read_conn, identifier)


def expressions_for_claim(
    store: TruthStore,
    claim_ref: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[ExpressionRecord, ...]:
    """Find every passage expressing a claim, for supersession staleness."""
    ref = _require_text(claim_ref, "claim_ref")
    candidates = {ref}
    if ref.startswith("wb-truth://"):
        parsed = parse_truth_uri(ref)
        candidates = {parsed.uri}
        if parsed.store_id == store.store_id:
            candidates.add(parsed.record_id)
    else:
        normalized = _valid_record_id(ref, "claim_ref")
        candidates = {normalized}

    def _collect(read_conn: sqlite3.Connection) -> tuple[ExpressionRecord, ...]:
        placeholders = ",".join("?" for _ in candidates)
        rows = read_conn.execute(
            f"SELECT * FROM expressions WHERE claim_ref IN ({placeholders}) "
            "ORDER BY created_at, id",
            tuple(candidates),
        ).fetchall()
        return tuple(ExpressionRecord(**dict(row)) for row in rows)

    if conn is not None:
        return _collect(conn)
    with store._read_connection() as read_conn:
        return _collect(read_conn)


def _claim_side_stale_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    expression: ExpressionRecord,
) -> bool:
    if expression.claim_ref_kind == "uri":
        parsed = parse_truth_uri(expression.claim_ref)
        if parsed.store_id != store.store_id:
            # A claim superseded in another scope stays portable, never an error.
            return False
        local_id = parsed.record_id
    else:
        local_id = expression.claim_ref
    claim = store._get_claim_locked(conn, local_id)
    if claim is None or claim.redacted_at is not None:
        return True
    if claim.canonical_sha256 != expression.claim_canonical_sha256:
        return True
    base = store._latest_status_locked(conn, local_id, include_overlay=False)
    if base is not None and base.status in TERMINAL_STATUSES:
        return True
    return False


def _span_side_stale_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    expression: ExpressionRecord,
) -> bool:
    span = store._get_document_span_locked(conn, expression.document_span_id)
    if span is None or span.span_sha256 != expression.span_sha256:
        return True
    document = store._get_document_locked(conn, span.document_id)
    if document is None:
        return True
    base_content = None
    if expression.meta_json:
        try:
            base_content = json.loads(expression.meta_json).get("base_content_sha256")
        except (json.JSONDecodeError, AttributeError):
            base_content = None
    if base_content is None:
        return False
    return document.content_sha256 != base_content


def stale_expressions(
    store: TruthStore,
    *,
    document_id: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[StaleExpression, ...]:
    """Report expressions whose claim-side or span-side fingerprint has drifted."""

    def _collect(read_conn: sqlite3.Connection) -> tuple[StaleExpression, ...]:
        stale: list[StaleExpression] = []
        for expression in _expressions_for_document_locked(
            store, read_conn, document_id
        ):
            claim_side = _claim_side_stale_locked(store, read_conn, expression)
            span_side = _span_side_stale_locked(store, read_conn, expression)
            if claim_side or span_side:
                stale.append(
                    StaleExpression(
                        expression=expression,
                        claim_side_stale=claim_side,
                        span_side_stale=span_side,
                    )
                )
        return tuple(stale)

    identifier = _valid_record_id(document_id, "document_id")
    if conn is not None:
        return _collect(conn)
    with store._read_connection() as read_conn:
        return _collect(read_conn)


__all__ = [
    "StaleExpression",
    "ensure_document_span",
    "expressions_for_claim",
    "expressions_for_document",
    "mark_expression",
    "stale_expressions",
]
