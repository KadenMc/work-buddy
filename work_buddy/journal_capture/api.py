"""Authenticated local HTTP API for production Journal capture."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)
from work_buddy.dashboard import local_identity_api
from work_buddy.dashboard.local_identity_api import require_human_authority_request
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.domain_service import RunningNoteDocumentService
from work_buddy.document_kernel.journal_projection import (
    FileDivergenceCapture,
    JournalProjectionAdapter,
    JournalProjectionWorker,
)
from work_buddy.document_kernel.pilot import RunningNotePilotService
from work_buddy.document_kernel.runtime_service import shared_document_kernel
from work_buddy.document_kernel.cowork_integration import reconcile_journal_documents
from work_buddy.document_kernel.redaction_dispatch import (
    CoworkDocumentSourceDispatcher,
)
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCaptureError,
    JournalCaptureValidationError,
)
from work_buddy.journal_capture.migration import JournalMigrationService
from work_buddy.journal_capture.projection import (
    capture_view,
    running_note_document_gesture_context,
    view_snapshot,
)
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.smart import configured_journal_smart_processor
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.paths import resolve
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.sources.errors import SourceError, public_error
from work_buddy.sources.ingress import (
    DomainCommand,
    HumanInputRequest,
    TrustedIngressContext,
    TrustedIngressService,
)
from work_buddy.sources.models import ActorRef, SourceRef, canonical_sha256
from work_buddy.sources.dispatch import SourceOutbox
from work_buddy.sources.resolve import resolve_and_reserve_source, resolve_source
from work_buddy.sources.store import SourceStore


journal_capture_blueprint = Blueprint("journal_capture", __name__)
logger = logging.getLogger(__name__)

_runtime_lock = threading.Lock()
_runtime: tuple[SourceStore, JournalCaptureStore, JournalCaptureService] | None = None
_recovery_complete = False


def _services() -> tuple[SourceStore, JournalCaptureStore, JournalCaptureService]:
    global _runtime, _recovery_complete
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                sources = SourceStore.create(resolve("stores/sources"))
                journals = JournalCaptureStore()

                def authoritative_log_writer(entry, *, stated_at):
                    # The canonical binding, not the feature flag, decides
                    # whether this day already belongs to Co-work. The closed
                    # flag only prevents advancing new entities.
                    actor = local_identity_api._authority().enrolled_actor()
                    principal = ActorRef(
                        issuer_authority_id=actor.issuer_authority_id,
                        subject="work-buddy-journal-service",
                        kind="service",
                        tenant_scope_id=actor.tenant_scope_id,
                    )
                    with JournalMigrationService(
                        vault_root=JournalContentAdapter().vault_root,
                        journal_store=journals,
                        source_store=sources,
                        principal=principal,
                        cutover_enabled=False,
                        kernel=_kernel(),
                    ) as migration:
                        return migration.append_log_capture(
                            entry,
                            stated_at=stated_at,
                        )

                service = JournalCaptureService(
                    journals,
                    JournalContentAdapter(),
                    smart_processor=configured_journal_smart_processor(
                        sources,
                        journals,
                    ),
                    authoritative_log_writer=authoritative_log_writer,
                )
                _runtime = (sources, journals, service)
    assert _runtime is not None
    if not _recovery_complete:
        # Reads may reopen the retained cohort while restore reconciliation is
        # pending, but startup recovery must not lease Sources work or invoke
        # document-domain reconciliation.  The first later mutation is fenced
        # independently by its store/dispatcher boundary.
        if source_foundation_read_only():
            return _runtime
        with _runtime_lock:
            if not _recovery_complete:
                require_source_foundation_writable("journal_capture.recovery")
                sources, _journals, service = _runtime
                # Use the identity adapter's single default-authority seam so
                # restart recovery and request authentication share one
                # installation namespace (and tests can inject it exactly).
                actor = local_identity_api._authority().enrolled_actor()
                principal = ActorRef(
                    issuer_authority_id=actor.issuer_authority_id,
                    subject="work-buddy-journal-service",
                    kind="service",
                    tenant_scope_id=actor.tenant_scope_id,
                )
                # A committed editor update changes an exact copy into a mixed
                # derivative. Reconcile that dependency before either Source
                # redaction consumer decides whether a broad scrub is safe.
                reconcile_journal_documents(
                    journal_store=service.store,
                    source_store=sources,
                    source_principal=principal,
                    vault_root=service.adapter.vault_root,
                )
                summary = JournalSourceDispatcher(
                    sources,
                    service,
                    service_principal=principal,
                ).drain()
                if summary.delivered or summary.failed or summary.deferred:
                    logger.info(
                        "Reconciled Journal source effects: delivered=%d failed=%d deferred=%d",
                        summary.delivered,
                        summary.failed,
                        summary.deferred,
                    )
                document_summary = CoworkDocumentSourceDispatcher(
                    sources,
                    service.store,
                    service_principal=principal,
                    vault_root=service.adapter.vault_root,
                ).drain()
                if (
                    document_summary.completed
                    or document_summary.failed
                    or document_summary.deferred
                ):
                    logger.info(
                        "Reconciled Co-work document redactions: completed=%d "
                        "failed=%d deferred=%d",
                        document_summary.completed,
                        document_summary.failed,
                        document_summary.deferred,
                    )
                _recovery_complete = True
    return _runtime


def register_routes(app) -> None:
    """Mount the production Journal capture API once."""

    app.register_blueprint(journal_capture_blueprint)


def _body() -> Mapping[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, Mapping):
        raise JournalCaptureValidationError("The Journal request must be a JSON object.")
    return value


def _canonical_gesture_context(body: Mapping[str, Any]) -> str:
    exact = body.get("exact_text")
    if not isinstance(exact, str):
        raise JournalCaptureValidationError("Enter something to save.")
    value = {
        "client_mutation_id": body.get("client_mutation_id"),
        "day_id": body.get("day_id"),
        "exact_text_sha256": hashlib.sha256(exact.encode("utf-8")).hexdigest(),
        "input_mode": body.get("input_mode", "unknown"),
        "mode": body.get("mode"),
        "schema": "wb.journal-capture-gesture/v1",
        "stated_at": body.get("stated_at"),
        "target_id": body.get("target_id"),
    }
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _subject(body: Mapping[str, Any]) -> str:
    mutation = body.get("client_mutation_id")
    if not isinstance(mutation, str) or not mutation:
        raise JournalCaptureValidationError("A stable capture key is required.")
    return f"journal-capture:{mutation}"


def _source_actor(value: Any) -> ActorRef:
    return ActorRef.from_dict(value.to_dict())


def _trusted_context(authority, *, mode: CaptureMode, context_sha256: str) -> TrustedIngressContext:
    actor = _source_actor(authority.principal.actor)
    tenant = actor.tenant_scope_id
    issuer = ActorRef(
        issuer_authority_id=actor.issuer_authority_id,
        subject="work-buddy-local-identity",
        kind="service",
        tenant_scope_id=tenant,
    )
    service = ActorRef(
        issuer_authority_id=actor.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=tenant,
    )
    fingerprint = canonical_sha256(
        {
            "schema": "wb.journal-human-authority/v1",
            "actor": actor.to_dict(),
            "issuer": issuer.to_dict(),
            "gesture_id": authority.gesture_id,
            "action": authority.action,
            "context_sha256": context_sha256,
            "assurance": authority.assurance,
        }
    )
    purposes = ["journal.materialize"]
    if mode is CaptureMode.SMART:
        purposes.append("journal.smart_processing")
    return TrustedIngressContext(
        issuer=issuer,
        issuer_version="local-identity/v1",
        inputter=actor,
        service_principal=service,
        tenant_scope_id=tenant,
        surface="work-buddy-journal",
        namespace="journal-quick-capture",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance=authority.assurance,
        authorization_fingerprint=fingerprint,
        permitted_purposes=tuple(purposes),
        gesture_receipt_id=authority.gesture_id,
        gesture_context_sha256=context_sha256,
    )


def _service_principal(authority) -> ActorRef:
    actor = _source_actor(authority.principal.actor)
    return ActorRef(
        issuer_authority_id=actor.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=actor.tenant_scope_id,
    )


def _authorization_expires_at(authority) -> str:
    return datetime.fromtimestamp(
        float(authority.principal.session_expires_at), UTC
    ).isoformat()


def _kernel() -> DocumentKernelClient:
    return shared_document_kernel()


@journal_capture_blueprint.get("/api/journal/view")
def journal_view():
    sources, store, service = _services()
    enrolled = local_identity_api._authority().enrolled_actor()
    principal = ActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    CoworkDocumentSourceDispatcher(
        sources,
        store,
        service_principal=principal,
        vault_root=service.adapter.vault_root,
    ).drain()
    reconcile_journal_documents(
        journal_store=store,
        source_store=sources,
        source_principal=principal,
        vault_root=service.adapter.vault_root,
    )
    return jsonify(
        {
            "ok": True,
            "view": view_snapshot(
                store,
                smart_processing_available=service.smart_processing_available,
                smart_processing_disclosure=service.smart_processing_disclosure,
            ),
        }
    )


@journal_capture_blueprint.get("/api/journal/captures/<capture_id>")
def journal_capture(capture_id: str):
    _sources, store, _service = _services()
    capture = store.get_capture(capture_id)
    if capture is None:
        return _error("journal_capture_not_found", "That capture is unavailable.", 404)
    return jsonify({"ok": True, "capture": capture_view(store, capture)})


@journal_capture_blueprint.post("/api/journal/captures")
def create_journal_capture():
    try:
        body = _body()
        context_sha = _canonical_gesture_context(body)
        authority = require_human_authority_request(
            action="journal.capture.submit",
            subject=_subject(body),
            context_sha256=context_sha,
        )
        target = CaptureTarget(str(body.get("target_id") or ""))
        mode = CaptureMode(str(body.get("mode") or ""))
        exact_text = body.get("exact_text")
        if not isinstance(exact_text, str):
            raise JournalCaptureValidationError("Enter something to save.")
        client_mutation_id = str(body.get("client_mutation_id") or "")
        day_id = str(body.get("day_id") or "")
        input_mode = str(body.get("input_mode") or "unknown")
        stated_at = body.get("stated_at")
        if stated_at is not None and not isinstance(stated_at, str):
            raise JournalCaptureValidationError("The capture time is invalid.")
        trusted = _trusted_context(authority, mode=mode, context_sha256=context_sha)
        sources, store, service = _services()
        command = DomainCommand(
            schema="wb.journal-capture/v1",
            target_domain="journal",
            command_type="journal.capture.materialize",
            parameters={
                "client_mutation_id": client_mutation_id,
                "day_id": day_id,
                "target_id": target.value,
                "mode": mode.value,
                "input_mode": input_mode,
                "stated_at": stated_at,
            },
            authorization_fingerprint=trusted.authorization_fingerprint,
            authorization_expires_at=_authorization_expires_at(authority),
        )
        commit = TrustedIngressService(sources).commit_human_input(
            trusted,
            HumanInputRequest(
                exact_content=exact_text,
                client_mutation_id=client_mutation_id,
                input_mode=input_mode,
                occurred_at=stated_at,
                command=command,
            ),
        )
        if commit.command_id is None or commit.effect_id is None:
            raise RuntimeError("journal_source_command_missing")
        current_effect = SourceOutbox(sources).get(commit.effect_id)
        if current_effect is not None and current_effect.status in {
            "pending",
            "retryable",
            "paused",
        }:
            SourceOutbox(sources).reauthorize(
                commit.effect_id,
                authorization_fingerprint=trusted.authorization_fingerprint,
                authorization_expires_at=_authorization_expires_at(authority),
            )
        capture_id = JournalSourceDispatcher(
            sources,
            service,
            service_principal=_service_principal(authority),
            worker_id="journal-http",
        ).deliver_exact(commit.effect_id)
        capture = store.get_capture(capture_id)
        if capture is None:
            raise RuntimeError("journal_capture_receipt_missing")
        response = jsonify(
            {
                "ok": True,
                "persisted": True,
                "deduplicated": commit.deduplicated,
                "capture": capture_view(store, capture),
            }
        )
        response.status_code = 200 if commit.deduplicated else 201
        return response
    except ValueError:
        return _error("journal_capture_invalid", "The capture options are invalid.", 400)
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except SourceError as exc:
        body, status = public_error(exc)
        return jsonify({"ok": False, "error": body}), status
    except Exception:
        logger.exception("Journal capture failed after request validation")
        return _error(
            "journal_capture_failed",
            "The capture could not be completed. Your text is still in the editor.",
            500,
            retryable=True,
        )


@journal_capture_blueprint.post("/api/journal/captures/<capture_id>/retry")
def retry_journal_capture(capture_id: str):
    try:
        sources, store, service = _services()
        capture = store.get_capture(capture_id)
        if capture is None:
            return _error("journal_capture_not_found", "That capture is unavailable.", 404)
        context_sha = hashlib.sha256(
            f"wb.journal-capture-retry/v1:{capture_id}:{capture.revision}".encode()
        ).hexdigest()
        authority = require_human_authority_request(
            action="journal.capture.retry",
            subject=f"journal-capture:{capture_id}",
            context_sha256=context_sha,
        )
        purpose = (
            "journal.smart_processing"
            if capture.mode is CaptureMode.SMART
            else "journal.materialize"
        )
        resolved = resolve_source(
            sources,
            source_ref=SourceRef.parse(capture.source_ref),
            representation_id=capture.representation_id,
            principal=_service_principal(authority),
            purpose=purpose,
        )
        exact = resolved.content.decode("utf-8")
        if capture.mode is CaptureMode.SMART:
            retry_context = _trusted_context(
                authority,
                mode=capture.mode,
                context_sha256=context_sha,
            )
            effect_type = (
                "auto_route"
                if capture.requested_target is CaptureTarget.AUTO
                else "smart_annotate"
            )
            store.reauthorize_effect(
                capture_id,
                effect_type,
                authorization_fingerprint=retry_context.authorization_fingerprint,
                authorization_expires_at=_authorization_expires_at(authority),
            )
            updated = service.process_smart(capture_id, exact_text=exact)
        else:
            updated = service.retry_materialization(capture_id, exact_text=exact)
        return jsonify({"ok": True, "capture": capture_view(store, updated)})
    except UnicodeDecodeError:
        return _error("journal_source_invalid", "The saved capture is not readable text.", 409)
    except (JournalCaptureError, SourceError, LocalIdentityError) as exc:
        if isinstance(exc, SourceError):
            body, status = public_error(exc)
            return jsonify({"ok": False, "error": body}), status
        return _error(
            getattr(exc, "code", "journal_retry_failed"),
            str(exc),
            getattr(exc, "status", 409),
            retryable=True,
        )


@journal_capture_blueprint.post(
    "/api/journal/running-notes/<entry_id>/open-in-cowork"
)
def open_running_note_in_cowork(entry_id: str):
    """Materialize one exact Running Note into its domain-bound Co-work doc.

    The route is the first user-testable authority cutover.  It accepts no
    content or actor fields: both the source bytes and canonical human actor
    are resolved inside trusted backend boundaries from durable records.
    """

    try:
        body = _body()
        expected_version = body.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise JournalCaptureValidationError("The Running Note version is invalid.")
        sources, store, service = _services()
        entry = store.get_entry(entry_id)
        if entry is None or entry.entry_kind is not CaptureTarget.RUNNING_NOTES:
            return _error("journal_note_not_found", "That Running Note is unavailable.", 404)
        if entry.resolution_state in {"deleted", "redacted"}:
            return _error("journal_note_not_found", "That Running Note is unavailable.", 404)
        if entry.version != expected_version:
            return _error(
                "journal_note_changed",
                "That Running Note changed. Refresh and try again.",
                409,
                retryable=True,
            )
        context_sha = running_note_document_gesture_context(entry)
        authority = require_human_authority_request(
            action="journal.running_note.open_in_cowork",
            subject=f"journal-running-note:{entry.entry_id}",
            context_sha256=context_sha,
        )
        existing = store.get_document_binding(entry.entry_id)
        if existing is not None and existing.state != "retired":
            return jsonify(
                {
                    "ok": True,
                    "deduplicated": True,
                    "document": dict(existing.inspection),
                    "coworkHref": existing.cowork_href,
                }
            )

        capture = store.get_capture(entry.capture_id)
        if capture is None:
            raise RuntimeError("journal_capture_receipt_missing")
        principal = _service_principal(authority)
        consumer_id = hashlib.sha256(
            f"journal-running-note-document:{entry.entry_id}".encode("utf-8")
        ).hexdigest()[:32]
        reserved = resolve_and_reserve_source(
            sources,
            source_ref=SourceRef.parse(entry.source_ref),
            representation_id=capture.representation_id,
            principal=principal,
            purpose="journal.materialize",
            consumer_domain="cowork_document",
            consumer_id=consumer_id,
            use_kind="exact_insertion",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
            expected_digest=entry.content_sha256,
        )
        actor_json = json.dumps(
            _source_actor(authority.principal.actor).to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        service_json = json.dumps(
            principal.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        kernel = _kernel()
        adapter = service.adapter
        pilot = RunningNotePilotService(
            documents=RunningNoteDocumentService(kernel=kernel),
            projections=JournalProjectionWorker(
                kernel=kernel,
                adapter=JournalProjectionAdapter(adapter.vault_root),
                divergence_capture=FileDivergenceCapture(
                    source_store=sources,
                    vault_root=adapter.vault_root,
                    principal=principal,
                ),
            ),
        )
        result = pilot.execute(
            vault_root=adapter.vault_root,
            entry_id=entry.entry_id,
            day_id=entry.day_id,
            domain_revision=f"journal-entry:{entry.version}",
            source_store=sources,
            reserved_source=reserved,
            actors={
                "selected_by": actor_json,
                "applied_by": service_json,
                "reviewed_by": None,
            },
            idempotency_key=f"journal-running-note-cowork:{entry.entry_id}:v{entry.version}",
            expected_initial_text=entry.markdown,
        )
        inspection = result.inspection()
        binding = result.document.binding
        stored = store.record_document_binding(
            entry_id=entry.entry_id,
            binding_id=binding.binding_id,
            store_id=binding.store_id,
            document_id=binding.document_id,
            change_id=result.document.change.change_id,
            source_consumer_id=consumer_id,
            source_usage_id=reserved.reservation.usage_id,
            cowork_href=result.cowork_href,
            content_authority_epoch=binding.content_authority_epoch,
            entry_version=entry.version,
            inspection=inspection,
            state=(
                "paused_diverged"
                if result.projection.status == "paused_diverged"
                else "current"
            ),
        )
        return (
            jsonify(
                {
                    "ok": True,
                    "deduplicated": False,
                    "document": dict(stored.inspection),
                    "coworkHref": stored.cowork_href,
                }
            ),
            201,
        )
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if exc.retryable else 400, retryable=exc.retryable)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except SourceError as exc:
        error_body, status = public_error(exc)
        return jsonify({"ok": False, "error": error_body}), status
    except Exception:
        logger.exception("Running Note Co-work materialization failed")
        return _error(
            "journal_note_cowork_failed",
            "Co-work could not open that Running Note. The Journal note was not changed.",
            500,
            retryable=True,
        )


def _error(
    code: str,
    message: str,
    status: int,
    *,
    retryable: bool = False,
):
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                },
            }
        ),
        status,
    )


__all__ = [
    "journal_capture_blueprint",
    "register_routes",
]
