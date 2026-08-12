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
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from ipaddress import ip_address
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request

from work_buddy.conversations import execution as conversation_execution
from work_buddy.cowork import (
    conversations,
    document_agent,
    feedback,
    lifecycle_lock,
    lifecycle_state,
    provenance,
    readiness,
    source_observation,
    transport,
)
from work_buddy.cowork.paths import (
    CoworkPathError,
    resolve_document_source_path,
    resolve_markdown_path,
)
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.dashboard import local_identity_api
from work_buddy.dashboard.local_identity_api import authenticate_request_session
from work_buddy.document_kernel.cowork_integration import (
    apply_bound_direct_push,
    current_domain_binding,
    project_bound_document,
)
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.paths import resolve
from work_buddy.security.local_identity import HumanAuthorityContext, LocalIdentityError
from work_buddy.sources import ActorRef as SourceActorRef
from work_buddy.sources import SourceRef, SourceStore
from work_buddy.cowork.proposal_applicability import (
    ProposalApplicability,
    assess_proposal_applicability,
    load_current_projection,
)
from work_buddy.truth import documents, expressions, proposals, queries
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.expressions import ensure_document_span
from work_buddy.truth.identity import canonical_json, parse_truth_uri, sha256_text
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import DocumentRecord, TruthStore

logger = logging.getLogger(__name__)

cowork_blueprint = Blueprint("cowork", __name__)

# The MCP decision path binds gestures to this fixed single-user constant. The
# dashboard surface must NOT reuse it: a real dashboard user threads through
# instead (I17). Kept here only to document the boundary it must not cross.
_MCP_HUMAN_REF = "work-buddy-user"
_AUTOMATIC_PASTE_MAX_CHARS = 599
_PROVENANCE_EXACT_MAX_CHARS = 1_000_000
_PROVENANCE_CONTEXT_MAX_CHARS = 2_048

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


def cowork_mutation_context_sha256(
    *,
    operation: str,
    store_id: str,
    document_id: str,
    body: Mapping[str, Any] | None,
) -> str:
    """Canonical exact-body digest shared by Co-work gesture-gated routes."""

    return sha256_text(
        canonical_json(
            {
                "body": {} if body is None else dict(body),
                "document_id": document_id,
                "operation": operation,
                "store_id": store_id,
            }
        )
    )


def _require_human_action(
    *,
    operation: str,
    store_id: str,
    document_id: str,
    body: Mapping[str, Any] | None,
) -> tuple[HumanAuthorityContext, Actor]:
    """Consume one exact local gesture and derive the Truth actor server-side."""

    authority = local_identity_api.require_human_authority_request(
        action=f"cowork.{operation}",
        subject=f"cowork-document:{store_id}:{document_id}",
        context_sha256=cowork_mutation_context_sha256(
            operation=operation,
            store_id=store_id,
            document_id=document_id,
            body=body,
        ),
    )
    return authority, Actor("human", authority.principal.actor.canonical_id)


def _local_identity_error(exc: LocalIdentityError):
    return local_identity_api._error(exc)


@cowork_blueprint.get("/api/truth/cowork/current-actor")
def api_cowork_current_actor():
    """Expose the immutable identity binding used by provenance ``Me`` values."""
    try:
        principal = authenticate_request_session()
    except LocalIdentityError as exc:
        return _local_identity_error(exc)
    return jsonify(
        provenance.actor_binding(
            Actor("human", principal.actor.canonical_id)
        )
    )


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


def _resolve_document(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
):
    try:
        document = documents.get_document(store, document_id, conn=conn)
    except InvariantViolation:
        return None, _fail("document does not exist", 404)
    return document, None


@contextmanager
def _read_snapshot_transaction(
    store: TruthStore,
) -> Iterator[sqlite3.Connection]:
    """Hold one explicit SQLite snapshot across an R2 ledger projection."""

    with store._read_connection() as conn:
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")


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
    return source_observation.observe_document_source_sha256(store, document)


def _drift_from_hash(document: DocumentRecord, current: str | None) -> str:
    if documents.source_is_detached(document):
        return "clean"
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
    applicability: ProposalApplicability,
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
        # Compatibility alias for older dashboard bundles.  New clients use
        # the typed target-level result below.
        "base_ok": applicability.applicable,
        "applicability": applicability.to_wire(),
        "status": "open",
        "fixes_ref": None,
        "claim_refs": refs,
        "created_at": proposal.created_at,
    }


_EXPRESSION_CLAIM_STATUSES = frozenset(
    {
        "confirmed",
        "needs_review",
        "proposed",
        "challenged",
        "rejected",
        "superseded",
        "retracted",
        "expired",
    }
)


def _local_expression_claim_id(store: TruthStore, expression) -> str | None:
    if expression.claim_ref_kind == "local":
        return expression.claim_ref
    try:
        parsed = parse_truth_uri(expression.claim_ref)
    except ValueError:
        return None
    if parsed.kind != "claim" or parsed.store_id != store.store_id:
        return None
    return parsed.record_id


def _expression_entries(
    store: TruthStore,
    expr_records,
    span_by_id,
    claim_states,
) -> list[dict]:
    if not expr_records:
        return []
    state_by_id = {state.claim.id: state for state in claim_states}
    entries: list[dict] = []
    for expression in expr_records:
        span = span_by_id.get(expression.document_span_id)
        quote = (span["quote_exact"] if span else "") or ""
        quote_anchor = (
            _quote_anchor(span["selector_json"])
            if span is not None
            else {"exact": quote, "prefix": "", "suffix": ""}
        )
        local_claim_id = _local_expression_claim_id(store, expression)
        claim_state = (
            None if local_claim_id is None else state_by_id.get(local_claim_id)
        )
        claim_status = None if claim_state is None else claim_state.status
        entries.append(
            {
                "expression_id": expression.id,
                "span_id": expression.document_span_id,
                "node_id_hint": None,
                "quote": quote,
                "quote_anchor": quote_anchor,
                "claim_ref": expression.claim_ref,
                "claim_status": (
                    claim_status
                    if claim_status in _EXPRESSION_CLAIM_STATUSES
                    else None
                ),
                "claim_kind": (
                    None if claim_state is None else claim_state.claim.claim_kind
                ),
            }
        )
    return entries


def _provenance_spans(span_rows) -> list[dict]:
    spans: list[dict] = []
    for row in span_rows:
        author_kind = row["author_kind"]
        if author_kind == "human":
            trust_state = "human"
        elif (
            author_kind == "agent_run"
            and str(row["author_ref"] or "").strip()
            and row["created_by_kind"] == "human"
        ):
            # Agent authorship alone is not confirmation. Accepted proposal
            # spans carry the proposing agent as author while the human who
            # accepted them remains the durable row creator.
            trust_state = "ai_confirmed"
        else:
            trust_state = None
        if trust_state is None:
            continue
        spans.append(
            {
                "span_id": row["id"],
                "quote": row["quote_exact"] or "",
                "quote_anchor": _quote_anchor(row["selector_json"]),
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
                resolve_document_source_path(store, document)
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
            observed_source_sha256 = _current_file_sha256(store, document)
            current_file_sha256 = (
                document.content_sha256
                if documents.source_is_detached(document)
                else observed_source_sha256
            )
            import_source_sha256 = (
                documents.retained_file_import_source_sha256(document.meta_json)
                if documents.source_is_detached(document)
                else None
            )
            entry = {
                "document_id": document.id,
                "path": document.path,
                "title": document.title or "",
                "document_class": document.document_class,
                "profile": document.document_class,
                "source_writeback": documents.source_writeback_policy(document),
                "lifecycle": lifecycle,
                "current_file_sha256": current_file_sha256,
                "import_source_sha256": import_source_sha256,
                "observed_source_file_sha256": observed_source_sha256,
                # Compatibility alias retained while clients move to the
                # explicit observed-source field above.
                "source_file_sha256": observed_source_sha256,
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
    with _read_snapshot_transaction(store) as conn:
        document, doc_error = _resolve_document(store, document_id, conn=conn)
        if doc_error:
            return doc_error
        open_props = proposals.open_proposals(
            store,
            document_id=document.id,
            conn=conn,
        )
        expr_records = expressions.expressions_for_document(
            store,
            document.id,
            conn=conn,
        )
        span_rows = conn.execute(
            "SELECT id, selector_json, quote_exact, author_kind, author_ref, "
            "created_by_kind, created_by_ref FROM document_spans "
            "WHERE document_id = ? ORDER BY created_at, id",
            (document.id,),
        ).fetchall()
        events = store._document_events_locked(conn, document.id)
        claim_states = (
            queries.resolve_claim_states(store, conn=conn)
            if expr_records
            else ()
        )
        lifecycle = documents.current_lifecycle(
            store,
            document.id,
            conn=conn,
        )
        document_readiness = readiness.classify_document(
            store,
            document,
            read_only=_is_read_only(),
            conn=conn,
        )
        readiness_view = document_readiness.to_dict()
        current_projection, projection_reason = load_current_projection(
            store,
            document,
            structured_head_sha256=readiness_view["structured_head_sha256"],
            conn=conn,
        )
        # The additive Verify/Co-think projection is part of this document
        # read. Keep every ledger read on the same explicit snapshot so the
        # client cannot observe a capability/result/configuration mixture
        # assembled from different database moments.
        from work_buddy.cowork.verify_projection import document_additions

        verify_additions = document_additions(
            store,
            document,
            read_only=_is_read_only(),
            document_readiness=document_readiness,
            active=lifecycle == "active",
            conn=conn,
        )
        authorship_attestations = provenance.list_attestations(
            store,
            document.id,
            conn=conn,
        )
        provenance_view = provenance.project_attestations(
            store,
            document.id,
            current_structured_head_sha256=readiness_view[
                "structured_head_sha256"
            ],
            conn=conn,
        )
    span_by_id = {row["id"]: row for row in span_rows}
    observed_source_sha256 = _current_file_sha256(store, document)
    current_file_sha256 = (
        document.content_sha256
        if documents.source_is_detached(document)
        else observed_source_sha256
    )
    import_source_sha256 = (
        documents.retained_file_import_source_sha256(document.meta_json)
        if documents.source_is_detached(document)
        else None
    )
    state = _drift_from_hash(document, current_file_sha256)
    payload = {
        "document_id": document.id,
        "store_id": store.store_id,
        "path": document.path,
        "title": document.title or "",
        "profile": document.document_class,
        "document_class": document.document_class,
        "source_writeback": documents.source_writeback_policy(document),
        "import_source_sha256": import_source_sha256,
        "observed_source_file_sha256": observed_source_sha256,
        "lifecycle": lifecycle,
        "initialization_state": readiness_view["initialization_state"],
        "structured_head_sha256": readiness_view["structured_head_sha256"],
        "projection_blob_available": readiness_view["projection_blob_available"],
        "permissions": readiness_view["permissions"],
        "disabled_reason": readiness_view["disabled_reason"],
        "hashes": {
            "ydoc_snapshot_sha256": document.ydoc_snapshot_sha256,
            "last_materialized_sha256": document.content_sha256,
            "current_file_sha256": current_file_sha256,
            "import_source_sha256": import_source_sha256,
            "observed_source_file_sha256": observed_source_sha256,
            # Compatibility alias retained while clients move to the
            # explicit observed-source field above.
            "source_file_sha256": observed_source_sha256,
        },
        "drift": {"state": state, "diff_available": False},
        "open_proposals": [
            _open_proposal_entry(
                item,
                document,
                applicability=assess_proposal_applicability(
                    item,
                    document,
                    structured_head_sha256=readiness_view[
                        "structured_head_sha256"
                    ],
                    current_projection=current_projection,
                    projection_unavailable_reason=projection_reason,
                ),
            )
            for item in open_props
        ],
        "expressions": _expression_entries(
            store,
            expr_records,
            span_by_id,
            claim_states,
        ),
        "provenance_spans": _provenance_spans(span_rows),
        "authorship_attestations": authorship_attestations,
        "provenance": provenance_view,
        "events_cursor": events[-1].id if events else "",
    }
    # Additive capability handshake. Older dashboard bundles ignore these
    # fields; newer bundles fail closed when an older server omits them.
    payload.update(verify_additions)
    return jsonify(payload)


@cowork_blueprint.get("/api/truth/doc/<document_id>/changes/<change_id>")
def api_doc_change_get(document_id: str, change_id: str):
    """Return content-free origin and causality for one recorded document change.

    This is the inspection target carried by source-backed Journal links.  The
    immutable source bytes stay behind the Sources authorization boundary; the
    response contains only identity, hashes, actors, and recorded assurances.
    """

    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
    try:
        causality = DocumentCausalityStore(store.paths.sidecar)
        change = causality.get_change(change_id)
    except (KeyError, ValueError):
        change = None
    if change is None or change.document_id != document.id:
        return _fail("That document change is unavailable.", 404)
    binding = (
        causality.get_binding(change.binding_id)
        if change.binding_id is not None
        else None
    )
    source = None
    if change.source_ref is not None:
        try:
            source_ref = SourceRef.parse(change.source_ref)
            source_store = SourceStore.create(resolve("stores/sources"))
            item = source_store.get_item(source_ref)
            representation = (
                source_store.get_representation(change.source_representation_id)
                if change.source_representation_id is not None
                else None
            )
            source = {
                "source_ref": source_ref.uri,
                "source_role": item.source_role if item is not None else "unknown",
                "fidelity": item.fidelity if item is not None else "unknown",
                "originating_surface": (
                    item.originating_surface if item is not None else "unknown"
                ),
                "provider_id": (
                    item.origin_ref.provider_id
                    if item is not None and item.origin_ref is not None
                    else None
                ),
                "lifecycle_state": (
                    item.lifecycle_state if item is not None else "unavailable"
                ),
                "content_sha256": change.source_content_sha256,
                "representation_id": change.source_representation_id,
                "media_type": (
                    representation.media_type if representation is not None else None
                ),
                "byte_length": (
                    representation.byte_length if representation is not None else None
                ),
                "copy_relation": (
                    "exact_copy"
                    if change.exact_copied_text_sha256 is not None
                    else "source_backed_change"
                ),
            }
        except Exception as exc:
            logger.warning(
                "Could not project source metadata for document change %s (%s)",
                change_id,
                getattr(exc, "code", type(exc).__name__),
            )
            source = {
                "source_ref": change.source_ref,
                "source_role": "unknown",
                "fidelity": "unknown",
                "originating_surface": "unknown",
                "provider_id": None,
                "lifecycle_state": "unavailable",
                "content_sha256": change.source_content_sha256,
                "representation_id": change.source_representation_id,
                "media_type": None,
                "byte_length": None,
                "copy_relation": (
                    "exact_copy"
                    if change.exact_copied_text_sha256 is not None
                    else "source_backed_change"
                ),
            }
    return jsonify(
        {
            "schema": "wb.cowork-document-change-inspection/v1",
            "store_id": store.store_id,
            "document_id": document.id,
            "change_id": change.change_id,
            "operation_kind": change.operation_kind,
            "committed_at": change.committed_at,
            "actors": json.loads(change.actors_json),
            "assurance": json.loads(change.assurance_json),
            "source": source,
            "binding": (
                None
                if binding is None
                else {
                    "binding_id": binding.binding_id,
                    "domain_namespace": binding.domain_namespace,
                    "domain_kind": binding.domain_kind,
                    "domain_entity_id": binding.domain_entity_id,
                    "role": binding.role,
                    "content_authority": binding.content_authority,
                    "content_authority_epoch": binding.content_authority_epoch,
                    "lifecycle": binding.lifecycle,
                }
            ),
            "heads": {
                "base_structured_head_sha256": (
                    change.base_structured_head_sha256
                ),
                "result_structured_head_sha256": (
                    change.result_structured_head_sha256
                ),
            },
        }
    )


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


def _execution_selection_from_state(
    state: conversation_execution.ConversationExecution,
):
    from work_buddy.agent_execution.models import AgentExecutionSelection

    return AgentExecutionSelection(
        provider_id=state.provider_id,
        model_id=state.model_id,
        provider_label=state.provider_label,
        model_label=state.model_label,
    )


def _project_execution(conversation_id: str | None):
    from work_buddy.agent_execution.registry import default_selection

    return conversation_execution.projected_execution(
        conversation_id,
        default_selection().to_dict(),
    )


def _pin_projected_execution(conversation_id: str):
    """Return the conversation target, pinning its displayed default once."""
    state = _project_execution(conversation_id)
    if state.persisted:
        return state
    return conversation_execution.set_execution(
        conversation_id,
        state.to_dict(),
        expected_revision=None,
    )


def _conversation_status(conversation_id: str) -> str | None:
    """Read lifecycle state without parsing unrelated conversation metadata."""
    from work_buddy.conversations.store import get_connection

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return None if row is None else str(row["status"])
    finally:
        conn.close()


def _execution_snapshot(
    conversation_id: str | None,
    *,
    refresh_catalog: bool = False,
) -> dict[str, object]:
    """Project the picker catalog plus the conversation's durable selection."""
    from work_buddy.agent_execution.registry import get_catalog

    state = _project_execution(conversation_id)
    catalog = get_catalog(refresh=refresh_catalog)
    providers = [provider.to_dict() for provider in catalog.providers]

    selected_provider = next(
        (
            provider
            for provider in providers
            if provider.get("id") == state.provider_id
        ),
        None,
    )
    if selected_provider is None:
        providers.append(
            {
                "id": state.provider_id,
                "label": state.provider_label,
                "available": False,
                "availability": "unavailable",
                "auth_mode": "",
                "models": [
                    {
                        "id": state.model_id,
                        "label": state.model_label,
                        "available": False,
                        "description": "",
                        "unavailable_reason": (
                            "This saved execution provider is no longer available."
                        ),
                        "is_default": False,
                    }
                ],
                "unavailable_reason": (
                    "This saved execution provider is no longer available."
                ),
            }
        )
    else:
        models = selected_provider.get("models")
        model_list = models if isinstance(models, list) else []
        if not any(
            model.get("id") == state.model_id
            for model in model_list
            if isinstance(model, dict)
        ):
            model_list.append(
                {
                    "id": state.model_id,
                    "label": state.model_label,
                    "available": False,
                    "description": "",
                    "unavailable_reason": (
                        "This saved model is no longer available."
                    ),
                    "is_default": False,
                }
            )
            selected_provider["models"] = model_list

    selection = state.to_dict()
    selection["revision"] = state.revision or ""
    return {
        "selection": selection,
        "providers": providers,
        "read_only": _is_read_only(),
    }


def _unreadable_execution_snapshot() -> dict[str, object]:
    """Represent known-corrupt authority after a user action already committed."""
    reason = "This chat’s saved provider and model choice couldn’t be read."
    provider_id = "execution-unavailable"
    model_id = "saved-selection-unreadable"
    return {
        "selection": {
            "schema_version": 1,
            "provider_id": provider_id,
            "model_id": model_id,
            "provider_label": "Unavailable",
            "model_label": "Saved selection couldn’t be read",
            "revision": "",
            "persisted": False,
        },
        "providers": [
            {
                "id": provider_id,
                "label": "Unavailable",
                "available": False,
                "availability": "unavailable",
                "auth_mode": "",
                "description": "",
                "models": [
                    {
                        "id": model_id,
                        "label": "Saved selection couldn’t be read",
                        "available": False,
                        "description": "",
                        "unavailable_reason": reason,
                        "is_default": False,
                    }
                ],
                "unavailable_reason": reason,
            }
        ],
        "read_only": True,
        "error": {
            "code": "execution_selection_corrupt",
            "message": reason,
        },
    }


def _execution_snapshot_after_committed_action(
    conversation_id: str,
) -> dict[str, object]:
    """Never obscure a durable user action with a later projection failure."""
    try:
        return _execution_snapshot(conversation_id)
    except conversation_execution.ConversationExecutionCorrupt:
        return _unreadable_execution_snapshot()


def _requested_execution(
    body: object,
    *,
    require_expected_revision: bool,
):
    """Validate one UI-supplied pair and return trusted labels plus its CAS."""
    from work_buddy.agent_execution.models import AgentExecutionSelection
    from work_buddy.agent_execution.registry import validate_selection

    if not isinstance(body, dict):
        if require_expected_revision:
            raise ValueError("A provider and model are required.")
        return None
    nested = body.get("execution")
    source = nested if isinstance(nested, dict) else body
    provider_id = source.get("provider_id")
    model_id = source.get("model_id")
    if provider_id is None and model_id is None and not require_expected_revision:
        return None
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id is required")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id is required")
    if require_expected_revision and "expected_revision" not in source:
        raise ValueError("expected_revision is required")
    raw_revision = source.get("expected_revision")
    if raw_revision in (None, ""):
        expected_revision = None
    elif isinstance(raw_revision, str):
        expected_revision = raw_revision
    else:
        raise ValueError("expected_revision must be a string")
    trusted = validate_selection(
        AgentExecutionSelection(
            provider_id=provider_id.strip(),
            model_id=model_id.strip(),
        ),
        refresh=True,
    )
    return trusted, expected_revision


def _execution_error(
    exc: Exception,
    *,
    status: int | None = None,
    code: str | None = None,
    retryable: bool | None = None,
    conversation_id: str | None = None,
    agent: document_agent.DocumentAgentStatus | None = None,
):
    from work_buddy.agent_execution.models import AgentExecutionError

    if isinstance(exc, conversation_execution.ConversationExecutionConflict):
        default_code = "execution_selection_changed"
        message = "The model choice changed in another window. Reload and try again."
        default_status = 409
    elif isinstance(exc, conversation_execution.ConversationExecutionCorrupt):
        default_code = "execution_selection_corrupt"
        message = "This chat's saved provider and model choice could not be read."
        default_status = 409
    elif isinstance(exc, AgentExecutionError):
        default_code = exc.error_code
        message = str(exc)
        default_status = 409
    elif isinstance(exc, ValueError):
        default_code = "invalid_execution_selection"
        message = str(exc)
        default_status = 400
    else:
        default_code = "execution_unavailable"
        message = "That provider or model is unavailable right now."
        default_status = 409
    response_status = default_status if status is None else status
    response_code = default_code if code is None else code
    if isinstance(exc, conversation_execution.ConversationExecutionCorrupt):
        response_retryable = False
    elif retryable is None:
        response_retryable = (
            isinstance(
                exc,
                conversation_execution.ConversationExecutionConflict,
            )
            or response_status >= 500
        )
    else:
        response_retryable = retryable
    payload: dict[str, object] = {
        "ok": False,
        "error": {
            "code": response_code,
            "message": message,
            "details": {},
            "retryable": response_retryable,
        },
    }
    if (
        conversation_id is not None
        and isinstance(
            exc,
            conversation_execution.ConversationExecutionConflict,
        )
    ):
        payload["conversation_id"] = conversation_id
        payload["execution"] = _execution_snapshot(conversation_id)
        if agent is not None:
            payload["agent"] = agent.to_dict()
    return jsonify(payload), response_status


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

    try:
        conversation_id = conversations.find_document_conversation(
            document_id=document.id,
            store_id=store.store_id,
        )
    except conversation_execution.ConversationExecutionCorrupt as exc:
        return _execution_error(exc)
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
    try:
        execution_snapshot = _execution_snapshot(
            conversation_id,
            refresh_catalog=(
                request.args.get("refresh_execution") == "1"
            ),
        )
    except conversation_execution.ConversationExecutionCorrupt as exc:
        return _execution_error(
            exc,
            conversation_id=conversation_id,
            agent=agent_status,
        )
    return jsonify(
        {
            "ok": True,
            "conversation_id": conversation_id,
            "agent": agent_status.to_dict(),
            "feedback": feedback_payload,
            "execution": execution_snapshot,
        }
    )


@cowork_blueprint.post("/api/truth/doc/<document_id>/conversation/bind")
def api_doc_conversation_bind(document_id: str):
    """Bind the conversation and its displayed model without running it.

    Opening Chat needs a durable conversation id so the shared conversation
    widget can mount, but selecting that pane is not itself a request to run a
    model. This endpoint owns the canonical binding and pins the model selection
    returned to the picker so the first authored turn cannot silently run on a
    different default. Agent startup remains attached to an authored turn (or
    another explicit action).
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    try:
        _require_human_action(
            operation="chat.bind",
            store_id=store.store_id,
            document_id=document_id,
            body={},
        )
    except LocalIdentityError as exc:
        return _local_identity_error(exc)

    from work_buddy.consent import user_initiated

    binding = None
    try:
        # Binding participates in the same cross-database lifecycle boundary
        # as conversation start. Retirement must not commit and then miss a
        # conversation created by a concurrent Chat-pane gesture.
        with lifecycle_lock.document_lifecycle_lock(
            store.store_id,
            document_id,
        ):
            document, doc_error = _resolve_document(store, document_id)
            if doc_error:
                return doc_error
            if documents.current_lifecycle(store, document.id) != "active":
                return _fail(
                    "Chat cannot be opened for a retired document.",
                    409,
                )
            if not document_surface_allowed(store, document):
                return _fail(
                    "This document is not available in Co-work for this folder.",
                    403,
                )
            with user_initiated("dashboard.cowork.conversation_bind"):
                binding = conversations.ensure_document_conversation(
                    document_id=document.id,
                    store_id=store.store_id,
                )
                bound_status = _conversation_status(binding.conversation_id)
                if bound_status is None or bound_status == "closed":
                    return _execution_error(
                        ValueError(
                            "This document's conversation is closed and "
                            "cannot be reopened."
                        ),
                        status=409,
                        code="conversation_closed",
                        retryable=False,
                        conversation_id=binding.conversation_id,
                    )
                consumer = document_agent.document_agent_consumer(
                    store.store_id,
                    document.id,
                )
                agent_status = document_agent.inspect_document_agent(
                    binding.conversation_id,
                    consumer=consumer,
                )
                feedback_payload = feedback.feedback_items(
                    store,
                    document_id=document.id,
                    conversation_id=binding.conversation_id,
                )
                # Binding is the configuration boundary: make the selection
                # shown by this response authoritative without claiming a
                # lease, fencing a driver, or invoking a model.
                _pin_projected_execution(binding.conversation_id)
                execution_snapshot = _execution_snapshot(binding.conversation_id)
    except conversation_execution.ConversationExecutionCorrupt as exc:
        return _execution_error(
            exc,
            conversation_id=(
                None if binding is None else binding.conversation_id
            ),
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
            "execution": execution_snapshot,
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
    request_body = request.get_json(silent=True)
    try:
        requested_execution = _requested_execution(
            request_body,
            require_expected_revision=False,
        )
    except Exception as exc:
        return _execution_error(exc)
    try:
        _require_human_action(
            operation="chat.start",
            store_id=store.store_id,
            document_id=document_id,
            body=request_body if isinstance(request_body, Mapping) else {},
        )
    except LocalIdentityError as exc:
        return _local_identity_error(exc)

    from work_buddy.consent import user_initiated

    binding = None
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
                from work_buddy.conversations.store import get_agent_lease

                bound_status = _conversation_status(binding.conversation_id)
                if bound_status is None or bound_status == "closed":
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
                    if requested_execution is None:
                        execution_state = _pin_projected_execution(
                            binding.conversation_id
                        )
                    else:
                        selected, expected_revision = requested_execution
                        current_execution = _project_execution(
                            binding.conversation_id
                        )
                        consumer = document_agent.document_agent_consumer(
                            store.store_id,
                            document.id,
                        )
                        selection_changed = (
                            current_execution.provider_id
                            != selected.provider_id
                            or current_execution.model_id != selected.model_id
                        )
                        if (
                            selection_changed
                            and get_agent_lease(
                                binding.conversation_id,
                                consumer,
                            )
                            is not None
                        ):
                            return (
                                jsonify(
                                    {
                                        "ok": False,
                                        "error": {
                                            "code": "execution_switch_required",
                                            "message": (
                                                "Choose the provider and model "
                                                "with Run with before restarting "
                                                "chat."
                                            ),
                                            "details": {},
                                            "retryable": False,
                                        },
                                        "conversation_id": (
                                            binding.conversation_id
                                        ),
                                        "execution": _execution_snapshot(
                                            binding.conversation_id
                                        ),
                                    }
                                ),
                                409,
                            )
                        execution_state = conversation_execution.set_execution(
                            binding.conversation_id,
                            selected.to_dict(),
                            expected_revision=expected_revision,
                        )
                except Exception as exc:
                    return _execution_error(
                        exc,
                        conversation_id=binding.conversation_id,
                    )
                try:
                    agent_status = document_agent.ensure_document_agent(
                        store_id=store.store_id,
                        document_id=document.id,
                        conversation_id=binding.conversation_id,
                        execution=_execution_selection_from_state(
                            execution_state
                        ),
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
    except conversation_execution.ConversationExecutionCorrupt as exc:
        return _execution_error(
            exc,
            conversation_id=(
                None if binding is None else binding.conversation_id
            ),
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
            "execution": _execution_snapshot(binding.conversation_id),
        }
    )


@cowork_blueprint.patch("/api/truth/doc/<document_id>/conversation/execution")
def api_doc_conversation_execution(document_id: str):
    """Select one provider/model pair and restart only an existing driver."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    request_body = request.get_json(silent=True)
    try:
        selected, expected_revision = _requested_execution(
            request_body,
            require_expected_revision=True,
        )
    except Exception as exc:
        return _execution_error(exc)
    try:
        _require_human_action(
            operation="chat.execution_select",
            store_id=store.store_id,
            document_id=document_id,
            body=request_body if isinstance(request_body, Mapping) else {},
        )
    except LocalIdentityError as exc:
        return _local_identity_error(exc)

    from work_buddy.consent import user_initiated
    from work_buddy.conversations.store import get_agent_lease

    binding = None
    try:
        with lifecycle_lock.document_lifecycle_lock(store.store_id, document_id):
            document, doc_error = _resolve_document(store, document_id)
            if doc_error:
                return doc_error
            if documents.current_lifecycle(store, document.id) != "active":
                return _fail(
                    "The model cannot be changed for a retired document.",
                    409,
                )
            if not document_surface_allowed(store, document):
                return _fail(
                    "This document is not available in Co-work for this folder.",
                    403,
                )
            with user_initiated("dashboard.cowork.execution_select"):
                binding = conversations.ensure_document_conversation(
                    document_id=document.id,
                    store_id=store.store_id,
                )
                bound_status = _conversation_status(binding.conversation_id)
                if bound_status is None or bound_status == "closed":
                    return _execution_error(
                        ValueError(
                            "This document's conversation is closed and "
                            "cannot change models."
                        ),
                        status=409,
                        code="conversation_closed",
                        retryable=False,
                        conversation_id=binding.conversation_id,
                    )
                current = _project_execution(binding.conversation_id)
                changed = (
                    current.provider_id != selected.provider_id
                    or current.model_id != selected.model_id
                )
                if changed and current.revision != expected_revision:
                    raise conversation_execution.ConversationExecutionConflict(
                        "execution_selection_changed"
                    )
                consumer = document_agent.document_agent_consumer(
                    store.store_id,
                    document.id,
                )
                previous_lease = get_agent_lease(
                    binding.conversation_id,
                    consumer,
                )
                should_restart = (
                    changed
                    and previous_lease is not None
                    and previous_lease.get("status") in {"starting", "running"}
                )
                if should_restart:
                    document_agent.fence_document_agent(
                        conversation_id=binding.conversation_id,
                        consumer=consumer,
                    )
                execution_state = conversation_execution.set_execution(
                    binding.conversation_id,
                    selected.to_dict(),
                    expected_revision=expected_revision,
                )
                if should_restart:
                    try:
                        agent_status = document_agent.ensure_document_agent(
                            store_id=store.store_id,
                            document_id=document.id,
                            conversation_id=binding.conversation_id,
                            execution=_execution_selection_from_state(
                                execution_state
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "Document-agent restart failed after execution "
                            "selection: store=%s document=%s conversation=%s",
                            store.store_id,
                            document.id,
                            binding.conversation_id,
                        )
                        agent_status = _conversation_spawn_failure()
                else:
                    agent_status = document_agent.inspect_document_agent(
                        binding.conversation_id,
                        consumer=consumer,
                    )
    except conversation_execution.ConversationExecutionConflict as exc:
        conflict_agent = document_agent.inspect_document_agent(
            binding.conversation_id,
            consumer=document_agent.document_agent_consumer(
                store.store_id,
                document.id,
            ),
        )
        return _execution_error(
            exc,
            conversation_id=binding.conversation_id,
            agent=conflict_agent,
        )
    except conversation_execution.ConversationExecutionCorrupt as exc:
        return _execution_error(
            exc,
            conversation_id=(
                None if binding is None else binding.conversation_id
            ),
        )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    return jsonify(
        {
            "ok": True,
            "conversation_id": binding.conversation_id,
            "created": binding.created,
            "execution": _execution_snapshot(binding.conversation_id),
            "agent": agent_status.to_dict(),
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
    compacted_projection = (
        request.headers.get("X-WB-Compacted-Projection-Sha256") or None
    )
    enrolled = local_identity_api._authority().enrolled_actor()
    source_principal = SourceActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    input_assurance = "legacy_dashboard_surface_unverified"
    try:
        local_principal = authenticate_request_session(require_csrf=True)
        actor = Actor("human", local_principal.actor.canonical_id)
        input_actor = local_principal.actor.canonical_id
        input_assurance = "enrolled_local_session"
    except LocalIdentityError:
        # Compatibility only.  An unauthenticated local editor update is
        # explicitly system/local-surface input, never human authorship.
        actor = Actor(
            "system",
            "work-buddy-local-surface",
            {"source_actor_kind": "local_surface"},
        )
        input_actor = json.dumps(
            {"kind": "local_surface", "ref": "work-buddy-local-surface"},
            sort_keys=True,
            separators=(",", ":"),
        )
    try:
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.ydoc_push"):
            bound = current_domain_binding(store, document.id)
            direct = None
            if (
                bound is not None
                and compacted is None
                and base_structured_head_sha256
                and base_ydoc_generation
            ):
                expected_snapshot = document.ydoc_snapshot_sha256

                def _bound_guard() -> None:
                    current = documents.get_document(store, document.id)
                    if documents.current_lifecycle(store, current.id) != "active":
                        raise InvariantViolation("document_retired")
                    if not document_surface_allowed(store, current):
                        raise InvariantViolation("policy_forbidden")
                    if current.ydoc_snapshot_sha256 != expected_snapshot:
                        raise InvariantViolation("structured_snapshot_changed")
                    if (
                        documents.current_ydoc_generation(store, current.id)
                        != base_ydoc_generation
                    ):
                        raise InvariantViolation("ydoc_generation_changed")

                direct = apply_bound_direct_push(
                    store,
                    document,
                    update=body,
                    expected_head=base_structured_head_sha256,
                    expected_generation=base_ydoc_generation,
                    actors={"input_by": input_actor},
                    input_assurance=input_assurance,
                    source_store=SourceStore.create(resolve("stores/sources")),
                    source_principal=source_principal,
                    lock_guard=_bound_guard,
                )
            if direct is not None:
                payload = {
                    "ok": True,
                    "applied": True,
                    "doc_sha256": document.content_sha256,
                    "projection_sha256": document.content_sha256,
                    "structured_head_sha256": direct.change.result_structured_head_sha256,
                    "ydoc_head_sha256": direct.change.result_structured_head_sha256,
                    "ydoc_generation": base_ydoc_generation,
                    "next_offset": direct.next_offset,
                    "document_change_id": direct.change.change_id,
                    "domain_projection_status": (
                        direct.projection.status if direct.projection is not None else "pending"
                    ),
                }
                status = 200
            else:
                payload, status = transport.push_ydoc(
                    store,
                    document,
                    actor,
                    body=body,
                    base_sha256=base_sha256,
                    base_structured_head_sha256=base_structured_head_sha256,
                    base_ydoc_generation=base_ydoc_generation,
                    compacted_snapshot_sha256=compacted,
                    compacted_projection_sha256=compacted_projection,
                )
                if bound is not None and status == 200 and payload.get("ok") is True:
                    cursor = project_bound_document(
                        store,
                        binding=bound,
                        change=None,
                        source_store=SourceStore.create(resolve("stores/sources")),
                        source_principal=source_principal,
                    )
                    payload["domain_projection_status"] = (
                        cursor.status if cursor is not None else "pending"
                    )
    except InvariantViolation as exc:
        return _fail(str(exc), 400)
    except RuntimeError as exc:
        code = str(exc)
        if code in {
            "direct_edit_base_conflict",
            "direct_edit_generation_conflict",
            "direct_edit_snapshot_conflict",
        }:
            return jsonify({"ok": False, "error": "stale_base"}), 409
        logger.exception("Bound Co-work document update failed (%s)", code)
        return _fail("The bound document update could not be completed.", 500)
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# Authorship and human-review attestations.
# ---------------------------------------------------------------------------


@cowork_blueprint.post("/api/truth/doc/<document_id>/authorship-attestations")
def api_doc_authorship_attestation(document_id: str):
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
    if not document_surface_allowed(store, document):
        return _fail(
            "This document is not available in Co-work for this folder.",
            403,
        )

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("request body must be a JSON object", 400)
    span = body.get("span")
    attestation = body.get("attestation")
    expected_head = body.get("expected_structured_head_sha256")
    idempotency_key = body.get("idempotency_key")
    basis_kind = body.get("basis_kind", "user_attestation")
    if not isinstance(span, dict):
        return _fail("span must be an object", 400)
    exact = span.get("exact")
    prefix = span.get("prefix", "")
    suffix = span.get("suffix", "")
    if not isinstance(exact, str) or not exact:
        return _fail("span.exact is required", 400)
    if not isinstance(prefix, str) or not isinstance(suffix, str):
        return _fail("span.prefix and span.suffix must be strings", 400)
    if len(exact) > _PROVENANCE_EXACT_MAX_CHARS:
        return _fail("span.exact is too large", 413)
    if (
        len(prefix) > _PROVENANCE_CONTEXT_MAX_CHARS
        or len(suffix) > _PROVENANCE_CONTEXT_MAX_CHARS
    ):
        return _fail("span context is too large", 413)
    if not isinstance(attestation, dict):
        return _fail("attestation must be an object", 400)
    if not isinstance(expected_head, str) or not expected_head:
        return _fail("expected_structured_head_sha256 is required", 400)
    if not isinstance(idempotency_key, str) or not idempotency_key:
        return _fail("idempotency_key is required", 400)
    if basis_kind not in {
        "automatic_short_text_attribution",
        "user_attestation",
    }:
        return _fail("basis_kind is invalid", 400)

    try:
        _authority_context, actor = _require_human_action(
            operation="provenance.attest",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
    except LocalIdentityError as exc:
        return _local_identity_error(exc)
    try:
        normalized_attestation = provenance.normalize_attestation(
            attestation,
            actor=actor,
        )
        if basis_kind == "automatic_short_text_attribution":
            expected_contributor = {
                "kind": "human",
                "ref": actor.ref,
            }
            contributors = normalized_attestation["authorship"]["contributors"]
            if (
                len(exact) > _AUTOMATIC_PASTE_MAX_CHARS
                or normalized_attestation["authorship"]["kind"] != "human"
                or len(contributors) != 1
                or contributors[0].get("kind") != expected_contributor["kind"]
                or contributors[0].get("ref") != expected_contributor["ref"]
                or normalized_attestation["human_review"]["status"]
                != "not_applicable"
                or normalized_attestation["human_review"]["reviewers"]
            ):
                return _fail(
                    "automatic short-text attribution is only available for "
                    "short text authored by the acting user",
                    400,
                )
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.provenance_attestation"):
            record, span_id = provenance.record_span_attestation(
                store,
                document_id=document.id,
                exact=exact,
                prefix=prefix,
                suffix=suffix,
                attestation=attestation,
                actor=actor,
                idempotency_key=idempotency_key,
                source={"kind": "paste", "format": "plain_text"},
                basis_kind=basis_kind,
                expected_structured_head_sha256=expected_head,
            )
    except provenance.ProvenanceActorBindingError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "details": {},
                        # The frozen determination must be revisited by the
                        # user; blind transport retry under another actor can
                        # never make this request safe.
                        "retryable": False,
                    },
                }
            ),
            exc.status,
        )
    except provenance.ProvenanceConflictError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                        "retryable": exc.retryable,
                    },
                }
            ),
            exc.status,
        )
    except InvariantViolation as exc:
        message = str(exc)
        stale = "target changed" in message
        forbidden = "not available in Co-work" in message
        conflict = any(
            marker in message
            for marker in (
                "retired document",
                "recovery",
                "pending",
                "idempotency_key",
            )
        )
        status = 403 if forbidden else 409 if stale or conflict else 400
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": (
                            "provenance_target_changed"
                            if stale
                            else "provenance_state_conflict"
                            if conflict
                            else "invalid_provenance_attestation"
                        ),
                        "message": message,
                        "details": {},
                        "retryable": stale or "pending" in message,
                    },
                }
            ),
            status,
        )

    _emit(
        "truth.doc_provenance_attested",
        store.store_id,
        {
            "document_id": document.id,
            "attestation_id": record.id,
            "document_span_id": span_id,
            "target_structured_head_sha256": (
                record.target_structured_head_sha256
            ),
            "basis_kind": record.basis_kind,
        },
        event_id=f"truth-doc-provenance-{record.id}",
    )
    return (
        jsonify(
            {
                "ok": True,
                "attestation_id": record.id,
                "document_span_id": span_id,
                "target_structured_head_sha256": (
                    record.target_structured_head_sha256
                ),
            }
        ),
        201,
    )


@cowork_blueprint.post(
    "/api/truth/doc/<document_id>/authorship-attestations/"
    "<attestation_id>/human-review"
)
def api_doc_provenance_human_review(
    document_id: str,
    attestation_id: str,
):
    """Append an exact human-review successor to one effective AI record."""

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
    if not document_surface_allowed(store, document):
        return _fail(
            "This document is not available in Co-work for this folder.",
            403,
        )
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("request body must be a JSON object", 400)
    if set(body) != {
        "attestation_id",
        "expected_structured_head_sha256",
        "idempotency_key",
    }:
        return _fail(
            "human review requires exactly attestation_id, "
            "expected_structured_head_sha256, and idempotency_key",
            400,
        )
    if body.get("attestation_id") != attestation_id:
        return _fail("attestation_id must match the route target", 400)
    expected_head = body.get("expected_structured_head_sha256")
    idempotency_key = body.get("idempotency_key")
    if not isinstance(expected_head, str) or not expected_head:
        return _fail("expected_structured_head_sha256 is required", 400)
    if not isinstance(idempotency_key, str) or not idempotency_key:
        return _fail("idempotency_key is required", 400)

    try:
        _authority_context, actor = _require_human_action(
            operation="provenance.review",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
    except LocalIdentityError as exc:
        return _local_identity_error(exc)
    try:
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.provenance_review"):
            record = provenance.record_human_review(
                store,
                document_id=document.id,
                attestation_id=attestation_id,
                actor=actor,
                idempotency_key=idempotency_key,
                expected_structured_head_sha256=expected_head,
            )
    except provenance.ProvenanceReviewError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                        "retryable": exc.retryable,
                    },
                }
            ),
            exc.status,
        )
    except (provenance.ProvenanceConflictError, InvariantViolation) as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "provenance_review_state_conflict",
                        "message": str(exc),
                        "details": {},
                        "retryable": False,
                    },
                }
            ),
            409,
        )

    _emit(
        "truth.doc_provenance_reviewed",
        store.store_id,
        {
            "document_id": document.id,
            "attestation_id": record.id,
            "supersedes_id": record.supersedes_id,
            "target_structured_head_sha256": (
                record.target_structured_head_sha256
            ),
        },
        event_id=f"truth-doc-provenance-review-{record.id}",
    )
    return (
        jsonify(
            {
                "ok": True,
                "attestation": provenance.portable_attestation(record),
            }
        ),
        201,
    )


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
    source_detached = documents.source_is_detached(document)
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
            "state": "clean" if source_detached else state.drift_state,
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
            "diff_available": not source_detached
            and state.baseline_available
            and state.current_file_sha256 is not None,
            "can_reimport": not source_detached
            and state.drift_state == "drifted"
            and not state.unmaterialized_structured_edits
            and state.initialization_state == "ready",
            "source_writeback": documents.source_writeback_policy(document),
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
    try:
        _authority_context, actor = _require_human_action(
            operation="feedback.capture",
            store_id=store.store_id,
            document_id=document_id,
            body=body,
        )
    except LocalIdentityError as exc:
        return _local_identity_error(exc)
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
                    execution_state = _pin_projected_execution(
                        capture.conversation_id
                    )
                    agent_status = document_agent.ensure_document_agent(
                        store_id=store.store_id,
                        document_id=document.id,
                        conversation_id=capture.conversation_id,
                        execution=_execution_selection_from_state(
                            execution_state
                        ),
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
            "execution": _execution_snapshot_after_committed_action(
                capture.conversation_id
            ),
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
    from work_buddy.cowork.chat_api import chat_blueprint
    from work_buddy.cowork.folder_api import cowork_folder_blueprint
    from work_buddy.cowork.materialization_api import materialization_blueprint
    from work_buddy.cowork.reimport_api import reimport_blueprint
    from work_buddy.cowork.retirement_api import retirement_blueprint
    from work_buddy.cowork.sitting_api import sitting_blueprint
    from work_buddy.cowork.truth_api import truth_blueprint
    from work_buddy.cowork.truth_analysis_api import truth_analysis_blueprint
    from work_buddy.cowork.verify_api import verify_blueprint

    app.register_blueprint(bootstrap_blueprint)
    app.register_blueprint(catalog_blueprint)
    app.register_blueprint(chat_blueprint)
    app.register_blueprint(cowork_folder_blueprint)
    app.register_blueprint(materialization_blueprint)
    app.register_blueprint(reimport_blueprint)
    app.register_blueprint(retirement_blueprint)
    app.register_blueprint(sitting_blueprint)
    app.register_blueprint(truth_blueprint)
    app.register_blueprint(truth_analysis_blueprint)
    app.register_blueprint(verify_blueprint)
    return app


__all__ = [
    "cowork_blueprint",
    "register_routes",
]
