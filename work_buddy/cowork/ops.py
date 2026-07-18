"""Agent-facing capabilities for the Co-work document surface.

These five operations are the entire agent-facing surface of a cowork doc.
Agents may list and read registered documents and PROPOSE work on them
(quote-anchored tracked edits, flags, and expression links). They never decide.
Accept, amend, reject, redirect, endorse, and defer are human gestures that
live only on the dashboard marks route, because an agent cannot approve its own
content.

Every operation calls the document engine (``work_buddy.truth.documents`` /
``proposals`` / ``expressions``) directly, exactly as the dashboard routes do.
The editing-kernel rule is enforced here: no operation writes a registered
document's file or prose. An agent contribution is always an open proposal a
human later decides.

Producer identity reuses the truth-ops session-manifest plumbing unchanged. The
gateway-injected ``agent_session_id`` resolves against the session manifest, and
a model claim that does not match a non-placeholder manifest model is durably
labeled ``caller_asserted`` rather than authenticated.

Gateway registration is intentionally external. ``register_ops`` binds these
five ops into the shared op registry, mirroring ``truth_ops``. The module also
calls it on import so any importer registers the surface.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from work_buddy.mcp_server.op_registry import register_op
from work_buddy.truth import documents, expressions, proposals
from work_buddy.truth.anchors import CompositeSelector, parse_selector
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.identity import new_id, sha256_bytes
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import EXPRESSION_ROLES, TruthStore


# --------------------------------------------------------------------------
# Store resolution and shared plumbing.
# --------------------------------------------------------------------------


def _registry() -> TruthStoreRegistry:
    return TruthStoreRegistry()


def _open_store(store_id: str) -> TruthStore:
    return _registry().open_store(store_id)


def _resolve_actor(
    producer_model: str,
    agent_session_id: str | None,
    producer_call_id: str | None,
) -> Any:
    """Derive the durable agent actor via the shared truth-ops plumbing.

    Imported lazily so importing this module carries no op-registration side
    effect from ``truth_ops`` and the module load order stays independent.
    """
    from work_buddy.mcp_server.ops.truth_ops import _agent_actor

    return _agent_actor(
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )


def _serialize(value: Any) -> Any:
    """Convert Truth models to gateway-safe JSON values (shared with truth-ops)."""
    from work_buddy.mcp_server.ops.truth_ops import _serialize as _truth_serialize

    return _truth_serialize(value)


def _require_document_surface(store: TruthStore) -> None:
    """Refuse every document op unless the scope opted into the surface.

    The ``document_surface`` profile block defaults to disabled, so a store that
    never opted in rejects list, get, propose, comment, and expression_mark
    with zero migration (the tables still exist, but the surface is inert).
    """
    if not store.profile.document_surface.enabled:
        raise InvariantViolation(
            "store profile does not enable the document_surface block"
        )


# --------------------------------------------------------------------------
# Anchor and payload helpers.
# --------------------------------------------------------------------------


def _selector_from_anchor(anchor: Any) -> tuple[CompositeSelector, str]:
    """Build a quote selector from a {exact, prefix, suffix} anchor mapping.

    A ``node_id_hint`` carried on the anchor is accepted but never used for
    anchoring. The quote anchor is resolved by the kernel anchors module, and
    node identity is ephemeral working state, never a durable key.
    """
    if not isinstance(anchor, Mapping):
        raise InvariantViolation("quote anchor must be a mapping with an exact quote")
    exact = anchor.get("exact")
    if not isinstance(exact, str) or not exact.strip():
        raise InvariantViolation("quote anchor requires a nonempty exact quote")
    selector = CompositeSelector(
        exact=exact,
        prefix=str(anchor.get("prefix") or ""),
        suffix=str(anchor.get("suffix") or ""),
    )
    return selector, exact


def _quote_anchor_view(selector_json: str) -> dict[str, str]:
    selector = parse_selector(selector_json)
    return {
        "exact": selector.exact,
        "prefix": selector.prefix,
        "suffix": selector.suffix,
    }


def _producer_view(meta_json: str | None) -> dict[str, Any] | None:
    if not meta_json:
        return None
    try:
        data = json.loads(meta_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, Mapping):
        return None
    return {
        key: data[key]
        for key in ("model", "harness", "surface", "session_id")
        if key in data
    }


def _claim_refs_view(claim_refs_json: str | None) -> list[dict[str, str]]:
    if not claim_refs_json:
        return []
    try:
        refs = json.loads(claim_refs_json)
    except (json.JSONDecodeError, TypeError):
        return []
    view: list[dict[str, str]] = []
    for ref in refs:
        if isinstance(ref, Mapping):
            view.append(
                {
                    "claim": ref.get("claim"),
                    "role": ref.get("role", "instantiation"),
                }
            )
    return view


def _current_file_sha256(store: TruthStore, path: str) -> str | None:
    target = store.paths.root / path
    if not target.is_file():
        return None
    return sha256_bytes(target.read_bytes())


def _proposal_view(
    proposal: Any,
    document: Any,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal.id,
        "kind": "edit" if proposal.replacement is not None else "flag",
        "quote_anchor": _quote_anchor_view(proposal.selector_json),
        "quote_exact": proposal.quote_exact,
        "replacement": proposal.replacement,
        "rationale": proposal.rationale,
        "tldr": proposal.tldr,
        "producer": _producer_view(proposal.meta_json),
        "base_doc_sha256": proposal.base_content_sha256,
        "canonical_sha256": proposal.canonical_sha256,
        "base_ok": proposal.base_content_sha256 == document.content_sha256,
        "claim_refs": _claim_refs_view(proposal.claim_refs_json),
        "created_at": proposal.created_at,
    }


def _expression_view(
    expression: Any,
    conn: Any,
    store: TruthStore,
) -> dict[str, Any]:
    span = store._get_document_span_locked(conn, expression.document_span_id)
    return {
        "expression_id": expression.id,
        "document_span_id": expression.document_span_id,
        "quote": None if span is None else span.quote_exact,
        "claim_ref": expression.claim_ref,
        "claim_ref_kind": expression.claim_ref_kind,
        "role": expression.role,
        "claim_canonical_sha256": expression.claim_canonical_sha256,
        "span_sha256": expression.span_sha256,
        "created_at": expression.created_at,
    }


# --------------------------------------------------------------------------
# Read capabilities.
# --------------------------------------------------------------------------


def cowork_doc_list(store_id: str, profile: str | None = None) -> dict[str, Any]:
    """List registered cowork docs with drift and open-proposal counts.

    ``open_proposal_count`` counts open edit proposals and ``open_flag_count``
    counts open flags (proposals with no replacement), so the two are disjoint
    and sum to the open total. The optional ``profile`` filter is coarse. One
    store carries exactly one profile, so a value that does not match the
    scope's profile yields an empty list rather than a per-document filter.
    """
    store = _open_store(store_id)
    _require_document_surface(store)
    if profile is not None and str(profile).strip() != store.profile.profile:
        return {
            "ok": True,
            "store_id": store.store_id,
            "profile": store.profile.profile,
            "count": 0,
            "docs": [],
        }
    docs_payload: list[dict[str, Any]] = []
    with store._read_connection() as conn:
        for document in documents.list_documents(store, conn=conn):
            current_file = _current_file_sha256(store, document.path)
            state = documents.drift_state(
                store,
                document.id,
                current_file_sha256=current_file,
                conn=conn,
            )
            open_props = proposals.open_proposals(
                store, document_id=document.id, conn=conn
            )
            edit_count = sum(1 for item in open_props if item.replacement is not None)
            flag_count = sum(1 for item in open_props if item.replacement is None)
            events = store._document_events_locked(conn, document.id)
            updated_at = events[-1].at if events else document.created_at
            docs_payload.append(
                {
                    "document_id": document.id,
                    "path": document.path,
                    "title": document.title,
                    "document_class": document.document_class,
                    "current_file_sha256": current_file,
                    "last_materialized_sha256": document.content_sha256,
                    "drift_state": state,
                    "open_proposal_count": edit_count,
                    "open_flag_count": flag_count,
                    "updated_at": updated_at,
                }
            )
    return {
        "ok": True,
        "store_id": store.store_id,
        "profile": store.profile.profile,
        "count": len(docs_payload),
        "docs": docs_payload,
    }


def cowork_doc_get(store_id: str, document_id: str) -> dict[str, Any]:
    """Read one cowork doc's content-meta, open proposals, expressions, and drift.

    Content itself rides the binary Y.Doc transport, so this carries meta plus
    the ledger-canonical review layer. Drift is the pure read projection, so a
    get never appends a doc_event. ``base_ok`` on each proposal reports whether
    it is still based on the current document content.
    """
    store = _open_store(store_id)
    _require_document_surface(store)
    with store._read_connection() as conn:
        document = documents.get_document(store, document_id, conn=conn)
        current_file = _current_file_sha256(store, document.path)
        state = documents.drift_state(
            store,
            document.id,
            current_file_sha256=current_file,
            conn=conn,
        )
        open_props = proposals.open_proposals(
            store, document_id=document.id, conn=conn
        )
        open_payload = [_proposal_view(item, document) for item in open_props]
        expr_payload = [
            _expression_view(expression, conn, store)
            for expression in expressions.expressions_for_document(
                store, document.id, conn=conn
            )
        ]
    return {
        "ok": True,
        "document_id": document.id,
        "store_id": store.store_id,
        "path": document.path,
        "title": document.title,
        "document_class": document.document_class,
        "hashes": {
            "ydoc_snapshot_sha256": document.ydoc_snapshot_sha256,
            "last_materialized_sha256": document.content_sha256,
            "current_file_sha256": current_file,
        },
        "drift": {"state": state, "diff_available": state == "drifted"},
        "open_proposals": open_payload,
        "expressions": expr_payload,
    }


# --------------------------------------------------------------------------
# Propose capabilities (normal weight, no decision authority).
# --------------------------------------------------------------------------


def cowork_doc_propose_edit(
    store_id: str,
    document_id: str,
    hunks: Sequence[Any],
    rationale: str,
    tldr: str,
    producer_model: str,
    base_doc_sha256: str | None = None,
    claim_refs: Sequence[Any] | None = None,
    meta: Mapping[str, Any] | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Open one edit proposal per hunk on a cowork doc.

    Each hunk is ``{quote_anchor:{exact,prefix,suffix}, replacement,
    node_id_hint?}``, and the shared ``rationale``, ``tldr``, and ``claim_refs``
    describe the edit. ``claim_refs`` is the one frozen shape, a list of
    ``{claim, role}`` with role defaulting to instantiation, carried through so
    accepting mints one expression per ref. A missing ``base_doc_sha256``
    defaults to the current document content, so a proposal is never
    accidentally stale at creation. ``meta`` is accepted and type-validated for
    forward compatibility. A v1 proposal row persists only producer identity, so
    additional meta keys are not stored, and producer identity cannot be
    overridden.
    """
    actor = _resolve_actor(producer_model, agent_session_id, producer_call_id)
    if not isinstance(hunks, Sequence) or isinstance(hunks, (str, bytes)):
        raise InvariantViolation("hunks must be a list of edit hunks")
    hunk_list = list(hunks)
    if not hunk_list:
        raise InvariantViolation("hunks must contain at least one edit")
    if meta is not None and not isinstance(meta, Mapping):
        raise InvariantViolation("meta must be a mapping")
    store = _open_store(store_id)
    _require_document_surface(store)
    document = documents.get_document(store, document_id)
    base = document.content_sha256 if base_doc_sha256 is None else base_doc_sha256
    all_records: list[Any] = []
    created_records: list[Any] = []
    for hunk in hunk_list:
        if not isinstance(hunk, Mapping):
            raise InvariantViolation("each hunk must be a mapping")
        selector, quote = _selector_from_anchor(hunk.get("quote_anchor"))
        replacement = hunk.get("replacement")
        if not isinstance(replacement, str) or not replacement.strip():
            raise InvariantViolation("each hunk requires a nonempty replacement")
        proposal_id = new_id()
        record = proposals.propose_edit(
            store,
            document_id=document.id,
            base_content_sha256=base,
            selector=selector,
            quote_exact=quote,
            replacement=replacement,
            rationale=rationale,
            tldr=tldr,
            claim_refs=claim_refs,
            actor=actor,
            proposal_id=proposal_id,
        )
        all_records.append(record)
        if record.id == proposal_id:
            created_records.append(record)
    events = [
        _serialize(
            emit_truth_event(
                "truth.doc_proposed",
                store_id=store.store_id,
                subject_kind="proposal",
                subject_id=record.id,
                data={
                    "document_id": document.id,
                    "proposal_id": record.id,
                    "kind": "edit" if record.replacement is not None else "flag",
                },
            )
        )
        for record in created_records
    ]
    return {
        "ok": True,
        "document_id": document.id,
        "created_count": len(created_records),
        "proposals": [_serialize(record) for record in all_records],
        "events": events,
    }


def cowork_doc_comment(
    store_id: str,
    document_id: str,
    quote_anchor: Mapping[str, Any],
    body: str,
    tldr: str,
    producer_model: str,
    base_doc_sha256: str | None = None,
    claim_refs: Sequence[Any] | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Raise a quote-anchored flag on a cowork doc.

    A flag is a proposal with no replacement. The ``body`` is the reviewer-facing
    problem being raised, and a missing ``base_doc_sha256`` defaults to the
    current document content.
    """
    actor = _resolve_actor(producer_model, agent_session_id, producer_call_id)
    if not isinstance(body, str) or not body.strip():
        raise InvariantViolation("comment body must be a nonempty string")
    selector, quote = _selector_from_anchor(quote_anchor)
    store = _open_store(store_id)
    _require_document_surface(store)
    document = documents.get_document(store, document_id)
    base = document.content_sha256 if base_doc_sha256 is None else base_doc_sha256
    proposal_id = new_id()
    record = proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=base,
        selector=selector,
        quote_exact=quote,
        replacement=None,
        rationale=body,
        tldr=tldr,
        claim_refs=claim_refs,
        actor=actor,
        proposal_id=proposal_id,
    )
    created = record.id == proposal_id
    event = None
    if created:
        event = _serialize(
            emit_truth_event(
                "truth.doc_proposed",
                store_id=store.store_id,
                subject_kind="proposal",
                subject_id=record.id,
                data={
                    "document_id": document.id,
                    "proposal_id": record.id,
                    "kind": "flag",
                },
            )
        )
    return {
        "ok": True,
        "document_id": document.id,
        "created": created,
        "proposal": _serialize(record),
        "event": event,
    }


def cowork_doc_expression_mark(
    store_id: str,
    document_id: str,
    span: Mapping[str, Any],
    claim_ref: str,
    role: str,
    producer_model: str,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Propose linking an existing passage to an existing claim (an expression).

    The ``role`` is required (the expression role column is not null) and states
    how the passage expresses the claim. The claim must resolve in this store,
    either as a local claim id or a ``wb-truth`` URI into this scope.
    """
    actor = _resolve_actor(producer_model, agent_session_id, producer_call_id)
    if not isinstance(role, str) or not role.strip():
        raise InvariantViolation("role is required")
    if role not in EXPRESSION_ROLES:
        raise InvariantViolation(
            f"role must be one of {sorted(EXPRESSION_ROLES)}"
        )
    if not isinstance(claim_ref, str) or not claim_ref.strip():
        raise InvariantViolation("claim_ref is required")
    selector, quote = _selector_from_anchor(span)
    store = _open_store(store_id)
    _require_document_surface(store)
    document = documents.get_document(store, document_id)
    document_span = expressions.ensure_document_span(
        store,
        document_id=document.id,
        selector=selector,
        quote_exact=quote,
        actor=actor,
    )
    expression = expressions.mark_expression(
        store,
        document_span_id=document_span.id,
        claim_ref=claim_ref,
        role=role,
        actor=actor,
    )
    event = _serialize(
        emit_truth_event(
            "truth.doc_expression_marked",
            store_id=store.store_id,
            subject_kind="expression",
            subject_id=expression.id,
            data={
                "document_id": document.id,
                "expression_id": expression.id,
                "claim_ref": claim_ref,
            },
        )
    )
    return {
        "ok": True,
        "document_id": document.id,
        "document_span": _serialize(document_span),
        "expression": _serialize(expression),
        "event": event,
    }


# --------------------------------------------------------------------------
# Registration.
# --------------------------------------------------------------------------


def register_ops(*, replace: bool = True) -> None:
    """Bind the cowork document ops into the shared op registry.

    Idempotent by default (``replace=True``) so the gateway wiring may call it
    more than once without a duplicate-registration error.
    """
    register_op("op.wb.cowork_doc_list", cowork_doc_list, replace=replace)
    register_op("op.wb.cowork_doc_get", cowork_doc_get, replace=replace)
    register_op(
        "op.wb.cowork_doc_propose_edit", cowork_doc_propose_edit, replace=replace
    )
    register_op("op.wb.cowork_doc_comment", cowork_doc_comment, replace=replace)
    register_op(
        "op.wb.cowork_doc_expression_mark",
        cowork_doc_expression_mark,
        replace=replace,
    )


register_ops()


__all__ = [
    "cowork_doc_comment",
    "cowork_doc_expression_mark",
    "cowork_doc_get",
    "cowork_doc_list",
    "cowork_doc_propose_edit",
    "register_ops",
]
