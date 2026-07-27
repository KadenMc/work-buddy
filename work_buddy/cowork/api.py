"""The `/api/truth/doc/*` route contract (R1-R10) for the Co-work surface.

Three BINDING rules shape this module (PRD section 8):

1. Sittings live on this dashboard HTTP surface EXCLUSIVELY. A multi-mark
   sitting cannot ride the per-invocation MCP decision ops, whose per-invocation
   branch would prompt inside a button click.
2. These routes call the ENGINE LIBRARY directly (the CLI's pattern), never
   wrapping the MCP decision ops. The button click is the consent boundary, so
   each mutating route wraps its dispatch in user_initiated and never re-prompts.
3. A real dashboard user identity threads into gesture actor refs, never the MCP
   path's fixed single-user constant (I17).

The routes here are a thin Flask adapter. Opaque Yjs framing lives in
transport.py and the R5 sitting decision policy lives in sittings.py, both
Flask-free so the engine seam stays testable on its own. The Flask mounting into
the dashboard service is a one-line join step (register_routes below), never an
edit to the dashboard service module.
"""

from __future__ import annotations

import json
import logging
from ipaddress import ip_address
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request

from work_buddy.cowork import (
    conversations,
    document_agent,
    feedback,
    lifecycle_lock,
    lifecycle_state,
    readiness,
    transport,
)
from work_buddy.cowork.paths import CoworkPathError, resolve_markdown_path
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.truth import documents, expressions, proposals
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.expressions import ensure_document_span
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import DocumentRecord, TruthStore

logger = logging.getLogger(__name__)

cowork_blueprint = Blueprint("cowork", __name__)

# The MCP decision path binds gestures to this fixed single-user constant. The
# dashboard surface must NOT reuse it: a real dashboard user threads through
# instead (I17). Kept here only to document the boundary it must not cross.
_MCP_HUMAN_REF = "work-buddy-user"

# Provenance trust state derives from durable span authorship: a human-authored
# span is human content, an agent-authored span reached durability only through
# acceptance, so it is confirmed. Proposed content is not yet a durable span.
_TRUST_BY_AUTHOR = {"human": "human", "agent_run": "ai_confirmed"}


# ---------------------------------------------------------------------------
# Store resolution, identity, gating, and small response helpers.
# ---------------------------------------------------------------------------


def _registry() -> TruthStoreRegistry:
    return TruthStoreRegistry()


def _open_store(store_id: str) -> TruthStore:
    return _registry().open_store(store_id)


def _is_read_only() -> bool:
    try:
        from work_buddy.config import load_config

        return bool(load_config().get("dashboard", {}).get("read_only", False))
    except Exception:  # noqa: BLE001 - a config failure never blocks a read route
        return False


def _reject_read_only():
    if _is_read_only():
        return jsonify({"ok": False, "error": "Dashboard is in read-only mode"}), 403
    return None


def dashboard_user_ref(headers=None) -> str:
    """Resolve the acting dashboard user ref, never the MCP single-user constant.

    A single local dashboard has no auth boundary, so the ref is threaded from an
    explicit request header, else configured dashboard identity, else a stable
    non-MCP default. The value is what lands on the gesture actor ref (I17).
    """
    if headers is not None:
        supplied = (headers.get("X-WB-User-Ref") or "").strip()
        if supplied and supplied != _MCP_HUMAN_REF:
            return supplied
    try:
        from work_buddy.config import load_config

        configured = str(
            (load_config().get("dashboard", {}) or {}).get("user_ref") or ""
        ).strip()
    except Exception:  # noqa: BLE001
        configured = ""
    if configured and configured != _MCP_HUMAN_REF:
        return configured
    return "dashboard-user"


def _actor_for_request() -> Actor:
    return Actor("human", dashboard_user_ref(request.headers))


def _fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _resolve_store(store_id: str | None):
    identifier = (store_id or "").strip()
    if not identifier:
        return None, _fail("store_id is required", 400)
    try:
        store = _open_store(identifier)
    except Exception:  # noqa: BLE001 - internal registry details stay server-side
        return None, _fail("That folder is not reachable by Co-work.", 404)
    return store, None


def _resolve_document(store: TruthStore, document_id: str):
    try:
        document = documents.get_document(store, document_id)
    except InvariantViolation:
        return None, _fail("document does not exist", 404)
    return document, None


def _document_surface_or_403(store: TruthStore, *, feedback: bool = False):
    policy = store.profile.document_surface
    if not policy.enabled:
        return _fail("Co-work documents are not enabled for this folder.", 403)
    if feedback and not policy.feedback_capture:
        return _fail("Feedback is not enabled for this folder.", 403)
    return None


def _emit(
    event_type: str,
    store_id: str,
    data: dict,
    *,
    event_id: str | None = None,
) -> None:
    try:
        emit_truth_event(
            event_type,
            store_id=store_id,
            data=data,
            event_id=event_id,
        )
    except Exception:  # noqa: BLE001 - events are non-authoritative and best effort
        logger.warning("cowork event emit failed: %s", event_type)


def _current_file_sha256(store: TruthStore, document: DocumentRecord) -> str | None:
    try:
        target = resolve_markdown_path(store, document.path).path
    except CoworkPathError:
        return None
    if not target.is_file():
        return None
    try:
        return sha256_bytes(target.read_bytes())
    except OSError:
        return None


def _drift_from_hash(document: DocumentRecord, current: str | None) -> str:
    if current is None:
        return "missing"
    return "clean" if current == document.content_sha256 else "drifted"


# ---------------------------------------------------------------------------
# R2 doc-open payload builders.
# ---------------------------------------------------------------------------


def _producer_view(meta_json: str | None) -> dict[str, str]:
    meta = json.loads(meta_json) if meta_json else {}
    # N2 alias: the wire producer.model_source denotes the kernel producer.harness
    # label. An explicit model_source (MCP verification source) wins when present.
    return {
        "model": str(meta.get("model") or ""),
        "model_source": str(meta.get("model_source") or meta.get("harness") or ""),
        "session_id": str(meta.get("session_id") or ""),
        "surface": str(meta.get("surface") or ""),
    }


def _quote_anchor(selector_json: str) -> dict[str, str]:
    selector = CompositeSelector.from_json(selector_json)
    return {
        "exact": selector.exact,
        "prefix": selector.prefix,
        "suffix": selector.suffix,
    }


def _open_proposal_entry(
    proposal,
    document: DocumentRecord,
    *,
    structured_head_sha256: str | None,
) -> dict:
    refs = json.loads(proposal.claim_refs_json) if proposal.claim_refs_json else []
    return {
        "proposal_id": proposal.id,
        "kind": "edit" if proposal.replacement is not None else "flag",
        "quote_anchor": _quote_anchor(proposal.selector_json),
        "replacement": proposal.replacement,
        "rationale": proposal.rationale or "",
        "tldr": proposal.tldr or "",
        "producer": _producer_view(proposal.meta_json),
        "epistemic_state": "ai_proposed",
        "base_doc_sha256": proposal.base_content_sha256,
        "base_structured_head_sha256": proposal.base_structured_head_sha256,
        "canonical_sha256": proposal.canonical_sha256,
        "base_ok": (
            proposal.base_content_sha256 == document.content_sha256
            and (
                proposal.base_structured_head_sha256 is None
                or proposal.base_structured_head_sha256
                == structured_head_sha256
            )
        ),
        "status": "open",
        "fixes_ref": None,
        "claim_refs": refs,
        "created_at": proposal.created_at,
    }


def _expression_entries(expr_records, span_by_id) -> list[dict]:
    entries: list[dict] = []
    for expression in expr_records:
        span = span_by_id.get(expression.document_span_id)
        entries.append(
            {
                "expression_id": expression.id,
                "span_id": expression.document_span_id,
                "node_id_hint": None,
                "quote": (span["quote_exact"] if span else "") or "",
                "claim_ref": expression.claim_ref,
                "claim_status": None,
                "claim_kind": None,
            }
        )
    return entries


def _provenance_spans(span_rows) -> list[dict]:
    spans: list[dict] = []
    for row in span_rows:
        trust_state = _TRUST_BY_AUTHOR.get(row["author_kind"])
        if trust_state is None:
            continue
        spans.append(
            {
                "span_id": row["id"],
                "quote": row["quote_exact"] or "",
                "trust_state": trust_state,
                "producer": None,
                "approval_gesture_id": None,
            }
        )
    return spans


# ---------------------------------------------------------------------------
# R1 list, R2 get.
# ---------------------------------------------------------------------------


@cowork_blueprint.get("/api/truth/doc/list")
def api_doc_list():
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document_class_filter = (
        request.args.get("document_class") or request.args.get("profile") or ""
    ).strip()
    read_only = _is_read_only()
    include_retired = request.args.get("include_retired") == "1"
    entries: list[dict] = []
    repairable_count = 0
    with store._read_connection() as conn:
        for document in documents.list_documents(
            store, include_retired=include_retired, conn=conn
        ):
            if (
                document_class_filter
                and document.document_class != document_class_filter
            ):
                continue
            open_props = proposals.open_proposals(
                store, document_id=document.id, conn=conn
            )
            events = store._document_events_locked(conn, document.id)
            lifecycle = documents.current_lifecycle(store, document.id, conn=conn)
            readiness_view = readiness.classify_document(
                store, document, read_only=read_only
            ).to_dict()
            try:
                resolve_markdown_path(store, document.path)
            except CoworkPathError:
                readiness_view.update(
                    {
                        "initialization_state": "corrupt",
                        "disabled_reason": "invalid_path",
                        "permissions": {
                            "open": False,
                            "edit": False,
                            "materialize": False,
                            "repair": False,
                            "retire": bool(
                                readiness_view["permissions"].get("retire")
                            ),
                        },
                    }
                )
            if readiness_view["permissions"]["repair"]:
                repairable_count += 1
            current_file_sha256 = _current_file_sha256(store, document)
            entry = {
                "document_id": document.id,
                "path": document.path,
                "title": document.title or "",
                "document_class": document.document_class,
                "profile": document.document_class,
                "lifecycle": lifecycle,
                "current_file_sha256": current_file_sha256,
                "last_materialized_sha256": document.content_sha256,
                "drift_state": (
                    "unknown"
                    if readiness_view["disabled_reason"] == "invalid_path"
                    else _drift_from_hash(document, current_file_sha256)
                ),
                "open_proposal_count": len(open_props),
                "open_flag_count": sum(
                    1 for item in open_props if item.replacement is None
                ),
                "updated_at": events[-1].at if events else document.created_at,
            }
            entry.update(readiness_view)
            entries.append(entry)
    return jsonify(
        {
            "store_id": store.store_id,
            "count": len(entries),
            "docs": entries,
            "repairable_count": repairable_count,
        }
    )


@cowork_blueprint.get("/api/truth/doc/<document_id>")
def api_doc_get(document_id: str):
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error

    open_props = proposals.open_proposals(store, document_id=document.id)
    expr_records = expressions.expressions_for_document(store, document.id)
    with store._read_connection() as conn:
        span_rows = conn.execute(
            "SELECT id, quote_exact, author_kind FROM document_spans "
            "WHERE document_id = ? ORDER BY created_at, id",
            (document.id,),
        ).fetchall()
        events = store._document_events_locked(conn, document.id)
    span_by_id = {row["id"]: row for row in span_rows}
    current_file_sha256 = _current_file_sha256(store, document)
    state = _drift_from_hash(document, current_file_sha256)
    readiness_view = readiness.classify_document(
        store, document, read_only=_is_read_only()
    ).to_dict()

    payload = {
        "document_id": document.id,
        "store_id": store.store_id,
        "path": document.path,
        "title": document.title or "",
        "profile": document.document_class,
        "document_class": document.document_class,
        "lifecycle": documents.current_lifecycle(store, document.id),
        "initialization_state": readiness_view["initialization_state"],
        "structured_head_sha256": readiness_view["structured_head_sha256"],
        "projection_blob_available": readiness_view["projection_blob_available"],
        "permissions": readiness_view["permissions"],
        "disabled_reason": readiness_view["disabled_reason"],
        "hashes": {
            "ydoc_snapshot_sha256": document.ydoc_snapshot_sha256,
            "last_materialized_sha256": document.content_sha256,
            "current_file_sha256": current_file_sha256,
        },
        "drift": {"state": state, "diff_available": False},
        "open_proposals": [
            _open_proposal_entry(
                item,
                document,
                structured_head_sha256=readiness_view[
                    "structured_head_sha256"
                ],
            )
            for item in open_props
        ],
        "expressions": _expression_entries(expr_records, span_by_id),
        "provenance_spans": _provenance_spans(span_rows),
        "events_cursor": events[-1].id if events else "",
    }
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Document conversation binding and explicit driver lifecycle.
# ---------------------------------------------------------------------------


def _conversation_spawn_failure() -> document_agent.DocumentAgentStatus:
    return document_agent.DocumentAgentStatus(
        status="spawn_failed",
        alive=False,
        started=False,
        error="Chat couldn’t start. Try again.",
    )


@cowork_blueprint.get("/api/truth/doc/<document_id>/conversation")
def api_doc_conversation_get(document_id: str):
    """Read the existing real binding and persisted driver status, without writes."""
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
    if not document_surface_allowed(store, document):
        return _fail("This document is not available in Co-work for this folder.", 403)

    conversation_id = conversations.find_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    )
    consumer = (
        None
        if conversation_id is None
        else document_agent.document_agent_consumer(store.store_id, document.id)
    )
    agent_status = document_agent.inspect_document_agent(
        conversation_id,
        consumer=consumer,
    )
    feedback_payload = feedback.feedback_items(
        store,
        document_id=document.id,
        conversation_id=conversation_id,
    )
    return jsonify(
        {
            "ok": True,
            "conversation_id": conversation_id,
            "agent": agent_status.to_dict(),
            "feedback": feedback_payload,
        }
    )


@cowork_blueprint.post("/api/truth/doc/<document_id>/conversation")
def api_doc_conversation_ensure(document_id: str):
    """Explicitly ensure the real binding and one generation-scoped driver."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate

    from work_buddy.consent import user_initiated

    try:
        # This lock is the cross-database lifecycle boundary.  It is acquired
        # before reading the Truth document and held through conversation bind
        # and driver ensure, so retirement cannot commit and miss a later bind.
        with lifecycle_lock.document_lifecycle_lock(
            store.store_id,
            document_id,
        ):
            document, doc_error = _resolve_document(store, document_id)
            if doc_error:
                return doc_error
            if documents.current_lifecycle(store, document.id) != "active":
                return _fail("Chat cannot be started for a retired document.", 409)
            if not document_surface_allowed(store, document):
                return _fail(
                    "This document is not available in Co-work for this folder.",
                    403,
                )
            with user_initiated("dashboard.cowork.conversation_start"):
                binding = conversations.ensure_document_conversation(
                    document_id=document.id,
                    store_id=store.store_id,
                )
                from work_buddy.conversations.store import get_conversation

                bound_conversation = get_conversation(binding.conversation_id)
                if (
                    bound_conversation is None
                    or bound_conversation.status == "closed"
                ):
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error": {
                                    "code": "conversation_closed",
                                    "message": (
                                        "This document's conversation is closed "
                                        "and cannot be restarted."
                                    ),
                                    "details": {},
                                    "retryable": False,
                                },
                            }
                        ),
                        409,
                    )
                try:
                    agent_status = document_agent.ensure_document_agent(
                        store_id=store.store_id,
                        document_id=document.id,
                        conversation_id=binding.conversation_id,
                    )
                except Exception:
                    logger.exception(
                        "Document-agent ensure failed after conversation binding: "
                        "store=%s document=%s conversation=%s",
                        store.store_id,
                        document.id,
                        binding.conversation_id,
                    )
                    agent_status = _conversation_spawn_failure()
                feedback_payload = feedback.feedback_items(
                    store,
                    document_id=document.id,
                    conversation_id=binding.conversation_id,
                )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    return jsonify(
        {
            "ok": True,
            "conversation_id": binding.conversation_id,
            "created": binding.created,
            "agent": agent_status.to_dict(),
            "feedback": feedback_payload,
        }
    )


# ---------------------------------------------------------------------------
# R3 / R4 Yjs transport (binary, application/octet-stream).
# ---------------------------------------------------------------------------


@cowork_blueprint.get("/api/truth/doc/<document_id>/ydoc")
def api_doc_ydoc_pull(document_id: str):
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
    since_offset = request.headers.get("X-WB-Since-Offset") or None
    try:
        body, headers = transport.pull_ydoc(
            store, document, since_offset=since_offset
        )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    response = Response(body, mimetype="application/octet-stream")
    for name, value in headers.items():
        response.headers[name] = value
    return response


@cowork_blueprint.post("/api/truth/doc/<document_id>/ydoc")
def api_doc_ydoc_push(document_id: str):
    blocked = _reject_read_only()
    if blocked:
        return blocked
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
    body = request.get_data(cache=False)
    base_sha256 = request.headers.get("X-WB-Base-Sha256") or None
    base_structured_head_sha256 = (
        request.headers.get("X-WB-Base-Ydoc-Sha256") or None
    )
    base_ydoc_generation = (
        request.headers.get("X-WB-Base-Ydoc-Generation") or None
    )
    compacted = request.headers.get("X-WB-Compacted-Snapshot-Sha256") or None
    actor = _actor_for_request()
    try:
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.ydoc_push"):
            payload, status = transport.push_ydoc(
                store,
                document,
                actor,
                body=body,
                base_sha256=base_sha256,
                base_structured_head_sha256=base_structured_head_sha256,
                base_ydoc_generation=base_ydoc_generation,
                compacted_snapshot_sha256=compacted,
            )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# R5 marks (the sitting).
# ---------------------------------------------------------------------------


@cowork_blueprint.post("/api/truth/doc/<document_id>/marks")
def api_doc_marks(document_id: str):
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": "two_phase_sitting_required",
                    "message": "Refresh Co-work before applying review decisions.",
                    "details": {
                        "prepare_route": f"/api/truth/doc/{document_id}/sitting/prepare"
                    },
                    "retryable": False,
                },
            }
        ),
        410,
    )


# ---------------------------------------------------------------------------
# R7 drift, R8 re-import.
# ---------------------------------------------------------------------------


@cowork_blueprint.get("/api/truth/doc/<document_id>/drift")
def api_doc_drift(document_id: str):
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
    state = lifecycle_state.inspect_lifecycle_state(store, document)
    baseline = state.materialized_version
    baseline_etag = (
        f'"{document.content_sha256}"' if state.baseline_available else None
    )
    source_etag = (
        None
        if state.current_file_sha256 is None
        else f'"{state.current_file_sha256}"'
    )
    return jsonify(
        {
            "ok": True,
            "state": state.drift_state,
            "last_materialized_sha256": document.content_sha256,
            "materialized_file_sha256": document.content_sha256,
            "current_file_sha256": state.current_file_sha256,
            "snapshot_sha256": document.ydoc_snapshot_sha256,
            "structured_head_sha256": state.structured_head_sha256,
            "materialized_snapshot_sha256": (
                None if baseline is None else baseline.ydoc_snapshot_sha256
            ),
            "materialized_structured_head_sha256": (
                None if baseline is None else baseline.structured_head_sha256
            ),
            "update_tail_present": state.update_tail_present,
            "unmaterialized_structured_edits": state.unmaterialized_structured_edits,
            "baseline": {
                "available": state.baseline_available,
                "sha256": document.content_sha256,
                "etag": baseline_etag,
                "source_url": f"/api/truth/doc/{document.id}/source?store_id={store.store_id}&version=materialized",
            },
            "source": {
                "available": state.current_file_sha256 is not None,
                "sha256": state.current_file_sha256,
                "etag": source_etag,
                "source_url": f"/api/truth/doc/{document.id}/source?store_id={store.store_id}&version=current",
            },
            "diff": None,
            "diff_available": state.baseline_available
            and state.current_file_sha256 is not None,
            "can_reimport": state.drift_state == "drifted"
            and not state.unmaterialized_structured_edits
            and state.initialization_state == "ready",
        }
    )


# ---------------------------------------------------------------------------
# R9 feedback capture.
# ---------------------------------------------------------------------------


@cowork_blueprint.post("/api/truth/doc/<document_id>/feedback")
def api_doc_feedback(document_id: str):
    blocked = _reject_read_only()
    if blocked:
        return blocked
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store, feedback=True)
    if gate:
        return gate
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("request body must be a JSON object", 400)
    span = body.get("span")
    text = body.get("text")
    if not isinstance(span, dict) or not isinstance(span.get("exact"), str):
        return _fail("span.exact is required", 400)
    if not isinstance(text, str) or not text.strip():
        return _fail("text is required", 400)
    actor = _actor_for_request()
    feedback_span = {
        "exact": span["exact"],
        "prefix": span.get("prefix") or "",
        "suffix": span.get("suffix") or "",
        "node_id_hint": span.get("node_id_hint"),
    }
    try:
        from work_buddy.consent import user_initiated

        # Feedback is human-AUTHORED content, not a gesture. capture_feedback saves
        # it VERBATIM as user_authored kernel evidence plus a document-span anchor,
        # and the feedback_poster hook posts it into the document's single
        # conversation, returning the conversation and message it landed in.
        # Keep the active check, authored persistence, conversation bind/message,
        # and driver ensure in one cross-process lifecycle critical section.
        with lifecycle_lock.document_lifecycle_lock(
            store.store_id,
            document_id,
        ):
            document, doc_error = _resolve_document(store, document_id)
            if doc_error:
                return doc_error
            if documents.current_lifecycle(store, document.id) != "active":
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": {
                                "code": "document_retired",
                                "message": (
                                    "Feedback cannot be added to a retired document."
                                ),
                                "details": {},
                            },
                        }
                    ),
                    409,
                )
            if not document_surface_allowed(store, document):
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": {
                                "code": "policy_forbidden",
                                "message": (
                                    "This document is not available in Co-work "
                                    "for this folder."
                                ),
                                "details": {},
                            },
                        }
                    ),
                    403,
                )
            with user_initiated("dashboard.cowork.feedback"):
                poster = conversations.feedback_poster(
                    document_id=document.id,
                    store_id=store.store_id,
                )
                capture = feedback.capture_feedback(
                    store,
                    document_id=document.id,
                    span=feedback_span,
                    verbatim_text=text,
                    actor=actor,
                    post_message=poster,
                )
                try:
                    agent_status = document_agent.ensure_document_agent(
                        store_id=store.store_id,
                        document_id=document.id,
                        conversation_id=capture.conversation_id,
                        feedback=document_agent.FeedbackPromptContext(
                            text=text,
                            exact=feedback_span["exact"],
                            prefix=feedback_span["prefix"],
                            suffix=feedback_span["suffix"],
                            message_id=capture.message_id,
                        ),
                    )
                except Exception:
                    # The authored turn, span, evidence, and event are already
                    # durable. Driver startup is a recoverable nested failure and
                    # must never turn successful feedback into a misleading error.
                    logger.exception(
                        "Document-agent ensure failed after feedback persisted: "
                        "store=%s document=%s conversation=%s",
                        store.store_id,
                        document.id,
                        capture.conversation_id,
                    )
                    agent_status = _conversation_spawn_failure()
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    return jsonify(
        {
            "ok": True,
            "evidence_id": capture.evidence_id,
            "span_id": capture.document_span_id,
            "conversation_id": capture.conversation_id,
            "message_id": capture.message_id,
            "agent": agent_status.to_dict(),
        }
    )


# ---------------------------------------------------------------------------
# Lifecycle retirement (history retained; source file is never deleted).
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# R10 register.
# ---------------------------------------------------------------------------


@cowork_blueprint.post("/api/truth/doc/register")
def api_doc_register():
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("request body must be a JSON object", 400)
    path = str(body.get("path") or "").strip()
    if not path:
        return _fail("path is required", 400)
    try:
        resolved = resolve_markdown_path(store, path)
    except CoworkPathError as exc:
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": "invalid_path",
                    "message": str(exc),
                    "field": "path",
                    "retryable": False,
                },
            }
        ), 422
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT document_id FROM document_path_keys WHERE path_key = ?",
            (resolved.path_key,),
        ).fetchone()
    if row is None:
        # Legacy R10 is a read-only compatibility lookup. Fresh registration
        # must use the exact two-phase bootstrap protocol so a document can
        # never be published with missing structured state.
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": "bootstrap_required",
                    "message": (
                        "This Markdown file must be registered through the "
                        "two-phase Co-work bootstrap flow."
                    ),
                    "retryable": False,
                    "details": {
                        "path": resolved.normalized,
                        "bootstrap_url": (
                            "/api/truth/doc/bootstrap"
                            f"?store_id={store.store_id}"
                        ),
                    },
                },
            }
        ), 409
    try:
        record = documents.get_document(store, str(row["document_id"]))
    except InvariantViolation as exc:
        return _fail(str(exc), 409)
    state = readiness.classify_document(
        store, record, read_only=_is_read_only()
    )
    if documents.current_lifecycle(store, record.id) == "retired":
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": "retired_path",
                    "message": "This Markdown path was removed from Co-work.",
                    "retryable": False,
                    "details": {"document_id": record.id},
                },
            }
        ), 409
    if state.initialization_state != "ready":
        status = 422 if state.initialization_state in {"corrupt", "semantic_corrupt"} else 409
        code = (
            "corrupt_document"
            if status == 422
            else state.initialization_state
        )
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": "The existing registration is not ready to open.",
                    "retryable": False,
                    "details": state.to_dict(),
                },
            }
        ), status
    current_file_sha256 = _current_file_sha256(store, record)
    return jsonify(
        {
            "ok": True,
            "document_id": record.id,
            "imported": False,
            "current_file_sha256": current_file_sha256,
            "readiness": state.to_dict(),
        }
    )


# ---------------------------------------------------------------------------
# Mounting (join step).
# ---------------------------------------------------------------------------


def _guard_cowork_host_access():
    """Keep host-filesystem Co-work APIs local until the dashboard has auth."""

    path = request.path
    protected = (
        path == "/api/truth/doc"
        or path.startswith("/api/truth/doc/")
        or path == "/api/truth/cowork"
        or path.startswith("/api/truth/cowork/")
    )
    if not protected:
        return None

    # `tailscale serve` and similar products proxy remote requests to Flask from
    # 127.0.0.1. Peer address alone is therefore not an authorization boundary.
    # Fail closed on proxy markers and require the browser-visible Host itself to
    # be local. A direct remote caller cannot bypass this with Host: localhost
    # because its socket peer remains non-loopback.
    proxy_markers = (
        "forwarded",
        "via",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    )
    proxied = any(request.headers.get(name) for name in proxy_markers) or any(
        name.lower().startswith("tailscale-") for name in request.headers.keys()
    )
    try:
        peer_is_loopback = ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        peer_is_loopback = False
    try:
        hostname = urlsplit(f"//{request.host}").hostname
        normalized_host = "" if hostname is None else hostname.rstrip(".").lower()
        host_is_loopback = normalized_host == "localhost" or ip_address(
            normalized_host
        ).is_loopback
    except ValueError:
        host_is_loopback = False
    if peer_is_loopback and host_is_loopback and not proxied:
        return None
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": "cowork_local_only",
                    "message": (
                        "Co-work can access host files and is available only "
                        "from this machine."
                    ),
                    "retryable": False,
                },
            }
        ),
        403,
    )


def register_routes(app):
    """Mount the co-work document blueprint onto a Flask app in one line."""
    app.before_request(_guard_cowork_host_access)
    app.register_blueprint(cowork_blueprint)
    from work_buddy.cowork.bootstrap_api import bootstrap_blueprint
    from work_buddy.cowork.catalog_api import catalog_blueprint
    from work_buddy.cowork.folder_api import cowork_folder_blueprint
    from work_buddy.cowork.materialization_api import materialization_blueprint
    from work_buddy.cowork.reimport_api import reimport_blueprint
    from work_buddy.cowork.retirement_api import retirement_blueprint
    from work_buddy.cowork.sitting_api import sitting_blueprint

    app.register_blueprint(bootstrap_blueprint)
    app.register_blueprint(catalog_blueprint)
    app.register_blueprint(cowork_folder_blueprint)
    app.register_blueprint(materialization_blueprint)
    app.register_blueprint(reimport_blueprint)
    app.register_blueprint(retirement_blueprint)
    app.register_blueprint(sitting_blueprint)
    return app


__all__ = [
    "cowork_blueprint",
    "dashboard_user_ref",
    "register_routes",
]
