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

from flask import Blueprint, Response, jsonify, request

from work_buddy.cowork import conversations, feedback, sittings, transport
from work_buddy.truth import documents, expressions, proposals
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.expressions import ensure_document_span
from work_buddy.truth.identity import sha256_bytes, sha256_text
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import DOCUMENT_CLASSES, DocumentRecord, TruthStore

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
    except Exception as exc:  # noqa: BLE001 - an unreachable store is a 404
        return None, _fail(f"truth store is not reachable: {exc}", 404)
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
        return _fail("document_surface is not enabled for this store", 403)
    if feedback and not policy.feedback_capture:
        return _fail("feedback_capture is not enabled for this store", 403)
    return None


def _emit(event_type: str, store_id: str, data: dict) -> None:
    try:
        emit_truth_event(event_type, store_id=store_id, data=data)
    except Exception:  # noqa: BLE001 - events are non-authoritative and best effort
        logger.warning("cowork event emit failed: %s", event_type)


def _current_file_sha256(store: TruthStore, document: DocumentRecord) -> str | None:
    target = store.paths.root / document.path
    if not target.is_file():
        return None
    return sha256_bytes(target.read_bytes())


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


def _open_proposal_entry(proposal, document: DocumentRecord) -> dict:
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
        "canonical_sha256": proposal.canonical_sha256,
        "base_ok": proposal.base_content_sha256 == document.content_sha256,
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
    profile_filter = (request.args.get("profile") or "").strip()
    entries: list[dict] = []
    with store._read_connection() as conn:
        for document in documents.list_documents(store, conn=conn):
            if profile_filter and document.document_class != profile_filter:
                continue
            open_props = proposals.open_proposals(
                store, document_id=document.id, conn=conn
            )
            events = store._document_events_locked(conn, document.id)
            entries.append(
                {
                    "document_id": document.id,
                    "path": document.path,
                    "title": document.title or "",
                    "profile": document.document_class,
                    "current_file_sha256": _current_file_sha256(store, document),
                    "last_materialized_sha256": document.content_sha256,
                    "drift_state": documents.drift_state(
                        store, document.id, conn=conn
                    ),
                    "open_proposal_count": len(open_props),
                    "open_flag_count": sum(
                        1 for item in open_props if item.replacement is None
                    ),
                    "updated_at": events[-1].at if events else document.created_at,
                }
            )
    return jsonify(
        {"store_id": store.store_id, "count": len(entries), "docs": entries}
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
    state = documents.drift_state(store, document.id)
    current_file_sha256 = _current_file_sha256(store, document)

    payload = {
        "document_id": document.id,
        "store_id": store.store_id,
        "path": document.path,
        "title": document.title or "",
        "profile": document.document_class,
        "hashes": {
            "ydoc_snapshot_sha256": document.ydoc_snapshot_sha256,
            "last_materialized_sha256": document.content_sha256,
            "current_file_sha256": current_file_sha256,
        },
        "drift": {"state": state, "diff_available": False},
        "open_proposals": [
            _open_proposal_entry(item, document) for item in open_props
        ],
        "expressions": _expression_entries(expr_records, span_by_id),
        "provenance_spans": _provenance_spans(span_rows),
        "events_cursor": events[-1].id if events else "",
    }
    return jsonify(payload)


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
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("request body must be a JSON object", 400)
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return _fail("items must be a non-empty list", 400)
    materialize = body.get("materialize")
    if materialize is not None and not isinstance(materialize, dict):
        return _fail("materialize must be an object or null", 400)
    actor = _actor_for_request()

    def _deliver_routing(verb: str, proposal_id: str, note: str | None):
        # A redirect or endorse keeps the proposal open and routes the human's
        # guidance into the document conversation for the proposing agent.
        return conversations.deliver_decision(
            document_id=document.id,
            store_id=store.store_id,
            verb=verb,
            proposal_id=proposal_id,
            note=note,
        )

    try:
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.marks"):
            response, events = sittings.apply_sitting(
                store,
                document,
                actor,
                items=items,
                materialize=materialize,
                deliver_routing=_deliver_routing,
            )
    except sittings.MaterializeHashMismatch:
        return jsonify({"ok": False, "error": "hash_mismatch"}), 409
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    for event_type, data in events:
        _emit(event_type, store.store_id, data)
    return jsonify(response)


# ---------------------------------------------------------------------------
# R6 materialize.
# ---------------------------------------------------------------------------


@cowork_blueprint.post("/api/truth/doc/<document_id>/materialize")
def api_doc_materialize(document_id: str):
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
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("request body must be a JSON object", 400)
    rendered = body.get("rendered_markdown")
    structured_doc_sha256 = str(body.get("structured_doc_sha256") or "").strip().lower()
    if not isinstance(rendered, str):
        return _fail("rendered_markdown must be a string", 400)
    # The one serializer is JavaScript and v1 has no Node runtime, so the client
    # block-splices to markdown and the server verifies the structured-doc hash
    # against the current Y.Doc snapshot, then writes through the engine (I14).
    if structured_doc_sha256 != (document.ydoc_snapshot_sha256 or ""):
        return jsonify({"ok": False, "error": "hash_mismatch"}), 409
    actor = _actor_for_request()
    new_file_sha256 = sha256_text(rendered)
    try:
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.materialize"):
            from work_buddy.artifacts.io import atomic_write_bytes

            target = store.paths.root / document.path
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, rendered.encode("utf-8"))
            event = documents.record_materialization(
                store,
                document_id=document.id,
                content_sha256=new_file_sha256,
                actor=actor,
            )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    _emit(
        "truth.doc_materialized",
        store.store_id,
        {"document_id": document.id, "file_sha256": new_file_sha256},
    )
    return jsonify(
        {
            "ok": True,
            "file_path": str(store.paths.root / document.path),
            "new_file_sha256": new_file_sha256,
            "front_matter_stamp": {
                "document_id": document.id,
                "content_sha256": new_file_sha256,
                "materialized_at": event.at,
            },
        }
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
    current_file_sha256 = _current_file_sha256(store, document)
    state = documents.drift_state(
        store, document.id, current_file_sha256=current_file_sha256
    )
    return jsonify(
        {
            "state": state,
            "last_materialized_sha256": document.content_sha256,
            "current_file_sha256": current_file_sha256,
            # A server-rendered redline needs the prior materialized text, which
            # v1 does not retain (no server serializer, C3/I14). The client owns
            # the live Y.Doc and renders redlines from it. The drift state and the
            # reimport gate below are what block silent regeneration (I13).
            "diff": None,
            "can_reimport": state == "drifted",
        }
    )


@cowork_blueprint.post("/api/truth/doc/<document_id>/reimport")
def api_doc_reimport(document_id: str):
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
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("request body must be a JSON object", 400)
    if not isinstance(body.get("structured_doc"), dict):
        return _fail("structured_doc must be the client-parsed document JSON", 400)
    file_sha256 = str(body.get("file_sha256") or "").strip().lower()
    if not file_sha256:
        return _fail("file_sha256 is required", 400)
    actor = _actor_for_request()
    try:
        from work_buddy.consent import user_initiated

        # The client parses the file once (MarkdownManager.parse) and posts JSON,
        # the server never HTML-parses. Out-of-band edits enter as an unattested
        # reimport change set, never a silent overwrite (I13). The proposals of
        # the change set are authored by the client via the propose caps.
        with user_initiated("dashboard.cowork.reimport"):
            event = documents.reimport_document(
                store,
                document_id=document.id,
                content_sha256=file_sha256,
                actor=actor,
            )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    _emit(
        "truth.doc_reimported",
        store.store_id,
        {"document_id": document.id, "change_set_id": event.id},
    )
    return jsonify({"ok": True, "change_set_id": event.id, "proposal_count": 0})


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
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
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
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    return jsonify(
        {
            "ok": True,
            "evidence_id": capture.evidence_id,
            "span_id": capture.document_span_id,
            "conversation_id": capture.conversation_id,
        }
    )


# ---------------------------------------------------------------------------
# R10 register.
# ---------------------------------------------------------------------------


@cowork_blueprint.post("/api/truth/doc/register")
def api_doc_register():
    blocked = _reject_read_only()
    if blocked:
        return blocked
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
    title = body.get("title")
    document_class = str(body.get("profile") or "").strip()
    if document_class not in DOCUMENT_CLASSES:
        return _fail(
            f"profile must be one of {sorted(DOCUMENT_CLASSES)}", 400
        )
    allowed = store.profile.document_surface.allowed_document_classes
    if allowed and document_class not in allowed:
        return _fail(
            f"document class {document_class!r} is not admitted by this store", 403
        )
    target = store.paths.root / path
    if not target.is_file():
        return _fail(f"file does not exist in scope: {path}", 404)
    content_sha256 = sha256_bytes(target.read_bytes())
    actor = _actor_for_request()
    try:
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.register"):
            existing = None
            with store._read_connection() as conn:
                existing = store._get_document_by_path_locked(conn, path)
            record = documents.register_document(
                store,
                path=path,
                title=title,
                document_class=document_class,
                content_sha256=content_sha256,
                actor=actor,
            )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    imported = existing is None
    if imported:
        _emit(
            "truth.doc_registered",
            store.store_id,
            {"document_id": record.id, "path": record.path},
        )
        _emit(
            "truth.doc_imported",
            store.store_id,
            {"document_id": record.id, "sha256": record.content_sha256},
        )
    return jsonify(
        {
            "ok": True,
            "document_id": record.id,
            "imported": imported,
            "current_file_sha256": content_sha256,
        }
    )


# ---------------------------------------------------------------------------
# Mounting (join step).
# ---------------------------------------------------------------------------


def register_routes(app):
    """Mount the co-work document blueprint onto a Flask app in one line."""
    app.register_blueprint(cowork_blueprint)
    return app


__all__ = [
    "cowork_blueprint",
    "dashboard_user_ref",
    "register_routes",
]
