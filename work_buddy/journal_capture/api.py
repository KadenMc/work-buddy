"""Authenticated local HTTP API for production Journal capture."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import UTC, date, datetime
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
from work_buddy.journal_capture.configuration import (
    JournalProfileConfigurationService,
)
from work_buddy.journal_capture.authority import JournalAuthorityCoordinator
from work_buddy.journal_capture.actions import (
    ITEM_ACTION_PURPOSE,
    PROMPT_INPUT_PURPOSE,
    PROMPT_RESULT_PURPOSE,
    JournalActionSourceService,
)
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.ingress import (
    JournalCaptureIngress,
    JournalIngressQueued,
)
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCaptureConflict,
    JournalCaptureError,
    JournalCaptureValidationError,
)
from work_buddy.journal_capture.native_source import JournalNativeSourceService
from work_buddy.journal_capture.migration import JournalMigrationService
from work_buddy.journal_capture.projection import (
    capture_view,
    current_day,
    field_value_view,
    native_item_view,
    running_note_document_gesture_context,
    view_snapshot,
)
from work_buddy.journal_capture.prompt_worker import (
    JournalPromptGenerationRunner,
    journal_service_principal,
)
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.smart import configured_journal_smart_processing
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.paths import resolve
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.sources.errors import SourceError, public_error
from work_buddy.sources.ingress import (
    HumanInputRequest,
    TrustedIngressContext,
    TrustedIngressService,
)
from work_buddy.sources.models import ActorRef, SourceRef, canonical_sha256
from work_buddy.sources.resolve import resolve_and_reserve_source, resolve_source
from work_buddy.sources.store import SourceStore


journal_capture_blueprint = Blueprint("journal_capture", __name__)
logger = logging.getLogger(__name__)

_runtime_lock = threading.Lock()
_runtime: tuple[SourceStore, JournalCaptureStore, JournalCaptureService] | None = None
_recovery_complete = False
_prompt_generation_runner = JournalPromptGenerationRunner()


def _services(
    *, recover: bool = True,
) -> tuple[SourceStore, JournalCaptureStore, JournalCaptureService]:
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

                from work_buddy.settings.broker import get_journal_smart_processing_enabled
                from work_buddy.threads.action_proposals import get_action_proposal_service

                def smart_configuration():
                    return configured_journal_smart_processing(
                        sources, journals, enabled=get_journal_smart_processing_enabled(),
                    )

                processor, availability = smart_configuration()
                service = JournalCaptureService(
                    journals,
                    JournalContentAdapter(),
                    smart_processor=processor,
                    smart_availability=availability,
                    smart_configuration=smart_configuration,
                    proposal_service=get_action_proposal_service(),
                    authoritative_log_writer=authoritative_log_writer,
                )
                _runtime = (sources, journals, service)
    assert _runtime is not None
    if recover and not _recovery_complete:
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
                authority = JournalAuthorityCoordinator(service.store).state()
                if (
                    authority.mode == "legacy_compatibility"
                    and authority.cutover_gate_state == "open"
                ):
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


_production_services = _services


def _read_service_from(
    writable_store: JournalCaptureStore,
    service: JournalCaptureService,
) -> tuple[JournalCaptureStore, JournalCaptureService]:
    """Clone an injected/runtime service over a read-only Journal connection."""

    store = JournalCaptureStore(writable_store.path, read_only=True)
    return store, JournalCaptureService(
        store,
        JournalContentAdapter(
            service.adapter.vault_root,
            journal_dir=service.adapter.journal_dir,
        ),
        smart_processor=service.smart_processor,
        proposal_service=service.proposal_service,
        smart_availability=service.smart_availability,
    )


def _read_store_and_service() -> tuple[JournalCaptureStore, JournalCaptureService]:
    """Open the Journal read model without recovery leases or writable SQLite."""

    if _runtime is not None:
        _sources, writable_store, service = _runtime
        return _read_service_from(writable_store, service)
    # Keep the long-standing test/application injection seam without letting a
    # cold production GET call `_services()` and create Sources or Journal
    # state.  An override is explicit; the production function is never called
    # from this read path.
    if _services is not _production_services:
        _sources, writable_store, service = _services()
        return _read_service_from(writable_store, service)
    path = resolve("db/journal-capture")
    if not path.is_file():
        raise JournalCaptureError("journal_capture_state_not_initialized")
    store = JournalCaptureStore(path, read_only=True)
    # Cold read-only startup intentionally does not inspect Settings/provider
    # state or create Sources.  A later explicit mutation/startup recovery owns
    # that work.
    return store, JournalCaptureService(store, JournalContentAdapter())


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
        **({"follow_up_action": body["follow_up_action"]} if body.get("follow_up_action") is not None else {}),
        **({"smart_disclosure_sha256": body["smart_disclosure_sha256"]} if body.get("smart_disclosure_sha256") is not None else {}),
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


def _profile_gesture_context(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(body), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


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


def _field_trusted_context(authority, *, context_sha256: str) -> TrustedIngressContext:
    """Bind one dashboard field edit to a retained, human-origin Source."""

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
            "schema": "wb.journal-field-human-authority/v1",
            "actor": actor.to_dict(),
            "issuer": issuer.to_dict(),
            "gesture_id": authority.gesture_id,
            "action": authority.action,
            "context_sha256": context_sha256,
            "assurance": authority.assurance,
        }
    )
    return TrustedIngressContext(
        issuer=issuer,
        issuer_version="local-identity/v1",
        inputter=actor,
        service_principal=service,
        tenant_scope_id=tenant,
        surface="work-buddy-journal",
        namespace="journal-field-value",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance=authority.assurance,
        authorization_fingerprint=fingerprint,
        permitted_purposes=("journal.field_value",),
        gesture_receipt_id=authority.gesture_id,
        gesture_context_sha256=context_sha256,
    )


def _action_trusted_context(
    authority,
    *,
    context_sha256: str,
    namespace: str,
    purpose: str,
) -> TrustedIngressContext:
    """Bind one explicit Journal action to its exact retained human input."""

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
            "schema": "wb.journal-action-human-authority/v1",
            "actor": actor.to_dict(),
            "issuer": issuer.to_dict(),
            "gesture_id": authority.gesture_id,
            "action": authority.action,
            "context_sha256": context_sha256,
            "assurance": authority.assurance,
            "namespace": namespace,
            "purpose": purpose,
        }
    )
    return TrustedIngressContext(
        issuer=issuer,
        issuer_version="local-identity/v1",
        inputter=actor,
        service_principal=service,
        tenant_scope_id=tenant,
        surface="work-buddy-journal",
        namespace=namespace,
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance=authority.assurance,
        authorization_fingerprint=fingerprint,
        permitted_purposes=(purpose,),
        gesture_receipt_id=authority.gesture_id,
        gesture_context_sha256=context_sha256,
    )


def _require_database_authority(store: JournalCaptureStore) -> None:
    if JournalAuthorityCoordinator(store).capture_mode() != "database_only":
        raise JournalCaptureConflict(
            "Journal content is read-only until database authority is active."
        )


def _mutation_fields(body: Mapping[str, Any]) -> tuple[str, int]:
    mutation_id = body.get("clientMutationId")
    expected_revision = body.get("expectedRevision")
    if (
        not isinstance(mutation_id, str)
        or not 8 <= len(mutation_id) <= 220
        or not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 1
    ):
        raise JournalCaptureValidationError(
            "That Journal action is invalid. Refresh and try again."
        )
    return mutation_id, expected_revision


def _field_value_id(
    *,
    local_date: str,
    module_instance_id: str,
    module_instance_version: int,
    composition_slot_id: str,
    field_id: str,
    field_definition_version: int,
) -> str:
    return "jfv_" + canonical_sha256(
        {
            "schema": "wb.journal-field-value-id/v1",
            "localDate": local_date,
            "module": [module_instance_id, module_instance_version],
            "compositionSlotId": composition_slot_id,
            "field": [field_id, field_definition_version],
        }
    )[:32]


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
    try:
        store, service = _read_store_and_service()
    except JournalCaptureError:
        return _error(
            "journal_not_initialized",
            "Journal is not initialized yet.",
            503,
            retryable=True,
        )
    local_date = request.args.get("day")
    if local_date is not None:
        try:
            parsed_day = date.fromisoformat(local_date)
        except ValueError:
            return _error("journal_day_invalid", "That Journal day is invalid.", 400)
        if parsed_day.isoformat() != local_date:
            return _error("journal_day_invalid", "That Journal day is invalid.", 400)
    return jsonify(
        {
            "ok": True,
            "view": view_snapshot(
                store,
                smart_processing_available=service.smart_processing_available,
                smart_processing_disclosure=service.smart_processing_disclosure,
                smart_availability=service.smart_availability,
                proposal_follow_ups=service.proposal_follow_ups,
                local_date=local_date,
            ),
        }
    )


@journal_capture_blueprint.get("/api/journal/captures/<capture_id>")
def journal_capture(capture_id: str):
    try:
        store, service = _read_store_and_service()
    except JournalCaptureError:
        return _error(
            "journal_not_initialized",
            "Journal is not initialized yet.",
            503,
            retryable=True,
        )
    capture = store.get_capture(capture_id)
    if capture is None:
        return _error("journal_capture_not_found", "That capture is unavailable.", 404)
    return jsonify({"ok": True, "capture": capture_view(store, capture, follow_ups=service.proposal_follow_ups(capture_id))})


@journal_capture_blueprint.get("/api/journal/profiles")
def journal_profiles():
    try:
        store, _service = _read_store_and_service()
    except JournalCaptureError:
        return _error(
            "journal_not_initialized",
            "Journal is not initialized yet.",
            503,
            retryable=True,
        )
    domain = JournalDomainService(store)
    profiles = domain.list_profiles()
    return jsonify(
        {
            "ok": True,
            "profiles": [
                {
                    "profileId": item.profile_id,
                    "profileRevision": item.profile_revision,
                    "formatVersion": item.format_version,
                    "name": item.name,
                    "description": item.description,
                    "canonicalOrder": list(item.canonical_order),
                    "profileDigest": item.profile_digest,
                    "createdBy": item.created_by,
                    "createdAt": item.created_at,
                    "supersedesRevision": item.supersedes_revision,
                }
                for item in profiles
            ],
        }
    )


@journal_capture_blueprint.get("/api/journal/configuration")
def journal_configuration():
    """Return private, typed Journal configuration metadata without mutation."""

    try:
        store, _service = _read_store_and_service()
        return jsonify(
            {"ok": True, "configuration": JournalProfileConfigurationService(store).catalog()}
        )
    except JournalCaptureError:
        return _error(
            "journal_not_initialized", "Journal is not initialized yet.", 503,
            retryable=True,
        )


@journal_capture_blueprint.post("/api/journal/configuration/preview")
def preview_journal_configuration():
    """Pure preview: validates a draft but stores no day/profile state."""

    try:
        body = _body()
        draft = body.get("draft")
        local_date = body.get("localDate")
        if not isinstance(draft, Mapping) or not isinstance(local_date, str):
            raise JournalCaptureValidationError("The Journal preview request is invalid.")
        store, _service = _read_store_and_service()
        preview = JournalProfileConfigurationService(store).preview(
            draft, local_date=local_date
        )
        return jsonify({"ok": True, "preview": preview})
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)


@journal_capture_blueprint.post("/api/journal/configuration/profiles")
def save_journal_configuration():
    """Save one immutable profile revision after an exact human gesture."""

    try:
        body = _body()
        draft = body.get("draft")
        mutation_id = body.get("clientMutationId")
        if not isinstance(draft, Mapping) or not isinstance(mutation_id, str):
            raise JournalCaptureValidationError("The Journal profile request is invalid.")
        profile_id = draft.get("profileId")
        if not isinstance(profile_id, str) or not profile_id:
            raise JournalCaptureValidationError("A Journal profile identity is required.")
        authority = require_human_authority_request(
            action="journal.profile.save",
            subject=f"journal-profile:{profile_id}",
            context_sha256=_profile_gesture_context(body),
        )
        _sources, store, _service = _services()
        domain = JournalDomainService(store)
        if domain.authority_state() == "recovery_fenced":
            return _error(
                "journal_recovery_fenced",
                "Journal recovery is still reconciling. Profile changes are paused.",
                409,
                retryable=True,
            )
        result = JournalProfileConfigurationService(store).save(
            draft,
            client_mutation_id=mutation_id,
            actor=_source_actor(authority.principal.actor).to_dict(),
        )
        return jsonify({"ok": True, "profile": result}), 201
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)


@journal_capture_blueprint.post(
    "/api/journal/configuration/profiles/<profile_id>/<int:profile_revision>/activate"
)
def activate_journal_configuration(profile_id: str, profile_revision: int):
    """Schedule a reviewed profile revision for an explicit future date."""

    try:
        body = _body()
        mutation_id = body.get("clientMutationId")
        effective = body.get("effectiveLocalDate")
        expected = body.get("expectedActivationRevision")
        if (
            not isinstance(mutation_id, str)
            or not isinstance(effective, str)
            or not isinstance(expected, int)
            or isinstance(expected, bool)
        ):
            raise JournalCaptureValidationError("The Journal activation request is invalid.")
        try:
            effective_date = date.fromisoformat(effective)
        except ValueError as exc:
            raise JournalCaptureValidationError("Choose a valid activation date.") from exc
        from work_buddy.config import USER_TZ

        if effective_date <= datetime.now(USER_TZ).date():
            raise JournalCaptureValidationError(
                "Choose a future date. Existing Journal days keep their current layout."
            )
        authority = require_human_authority_request(
            action="journal.profile.activate",
            subject=f"journal-profile:{profile_id}:{profile_revision}",
            context_sha256=_profile_gesture_context(body),
        )
        _sources, store, _service = _services()
        domain = JournalDomainService(store)
        if domain.authority_state() == "recovery_fenced":
            return _error(
                "journal_recovery_fenced",
                "Journal recovery is still reconciling. Profile changes are paused.",
                409,
                retryable=True,
            )
        activation_revision = domain.activate_profile(
            profile_id=profile_id,
            profile_revision=profile_revision,
            effective_local_date=effective_date.isoformat(),
            expected_activation_revision=expected,
            client_mutation_id=mutation_id,
            actor=_source_actor(authority.principal.actor).to_dict(),
        )
        return jsonify(
            {
                "ok": True,
                "activation": {
                    "profileId": profile_id,
                    "profileRevision": profile_revision,
                    "effectiveLocalDate": effective_date.isoformat(),
                    "activationRevision": activation_revision,
                },
            }
        )
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)


@journal_capture_blueprint.post("/api/journal/field-values")
def put_journal_field_value():
    """Retain one exact human field input, then publish its typed value."""

    try:
        body = _body()
        context_sha = _profile_gesture_context(body)
        mutation_id = body.get("clientMutationId")
        local_date = body.get("localDate")
        module_id = body.get("moduleInstanceId")
        module_version = body.get("moduleInstanceVersion")
        field_id = body.get("fieldId")
        field_version = body.get("fieldDefinitionVersion")
        slot_id = body.get("compositionSlotId")
        expected_revision = body.get("expectedRevision")
        exact_input = body.get("exactInput")
        stated_at = body.get("statedAt")
        requested_value_id = body.get("valueId")
        disposition = body.get("disposition")
        if (
            not isinstance(mutation_id, str)
            or not 8 <= len(mutation_id) <= 220
            or not isinstance(local_date, str)
            or not isinstance(module_id, str)
            or not module_id
            or not isinstance(module_version, int)
            or isinstance(module_version, bool)
            or module_version < 1
            or not isinstance(field_id, str)
            or not field_id
            or not isinstance(field_version, int)
            or isinstance(field_version, bool)
            or field_version < 1
            or not isinstance(slot_id, str)
            or not slot_id
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or not isinstance(exact_input, str)
            or len(exact_input) > 100_000
            or (stated_at is not None and not isinstance(stated_at, str))
            or (
                requested_value_id is not None
                and (not isinstance(requested_value_id, str) or not requested_value_id)
            )
            or disposition not in {None, "missing", "skipped", "declined"}
        ):
            raise JournalCaptureValidationError(
                "That Journal field edit is invalid. Refresh and try again."
            )
        try:
            parsed_local_date = date.fromisoformat(local_date)
        except ValueError as exc:
            raise JournalCaptureValidationError(
                "That Journal day is invalid."
            ) from exc
        if parsed_local_date.isoformat() != local_date:
            raise JournalCaptureValidationError("That Journal day is invalid.")
        authority = require_human_authority_request(
            action="journal.field_value.put",
            subject=f"journal-field:{local_date}:{module_id}:{field_id}",
            context_sha256=context_sha,
        )
        sources, store, _service = _services()
        if JournalAuthorityCoordinator(store).capture_mode() != "database_only":
            raise JournalCaptureConflict(
                "Typed Journal fields are read-only until database authority is active."
            )
        domain = JournalDomainService(store)
        day = current_day(local_date)
        composition = domain.resolve_day(
            local_date=local_date,
            timezone=day["timezone"],
            boundary=day["dayBoundaryStart"],
            window_start=day["windowStart"],
            window_end=day["windowEnd"],
        )
        module = next(
            (
                candidate
                for candidate in composition.modules
                if candidate.semantic_membership == "included"
                and candidate.module.module_instance_id == module_id
                and candidate.module.instance_version == module_version
            ),
            None,
        )
        field = next(
            (
                candidate
                for candidate in composition.fields
                if module is not None
                and candidate.module_slot_id == module.slot_id
                and candidate.composition_slot_id == slot_id
                and candidate.field_id == field_id
                and candidate.field_definition_version == field_version
            ),
            None,
        )
        if module is None or field is None:
            raise JournalCaptureConflict(
                "That Journal field is no longer part of this day. Refresh and try again."
            )
        value_id = requested_value_id or _field_value_id(
            local_date=local_date,
            module_instance_id=module_id,
            module_instance_version=module_version,
            composition_slot_id=slot_id,
            field_id=field_id,
            field_definition_version=field_version,
        )
        prior = next(
            (item for item in domain.list_field_values(local_date) if item.value_id == value_id),
            None,
        )
        current_revision = 0 if prior is None else prior.current_revision
        with store._connect() as conn:
            mutation_replay = conn.execute(
                "SELECT 1 FROM journal_mutations WHERE client_mutation_id=?",
                (mutation_id,),
            ).fetchone() is not None
        if not mutation_replay and current_revision != expected_revision:
            raise JournalCaptureConflict(
                "That Journal field changed. Refresh before saving your edit."
            )
        trusted = _field_trusted_context(authority, context_sha256=context_sha)
        commit = TrustedIngressService(sources).commit_human_input(
            trusted,
            HumanInputRequest(
                exact_content=exact_input,
                client_mutation_id=f"{mutation_id}:source",
                input_mode="direct_entry",
                occurred_at=stated_at,
            ),
        )
        frozen = domain.ensure_day(
            local_date=local_date,
            timezone=day["timezone"],
            boundary=day["dayBoundaryStart"],
            window_start=day["windowStart"],
            window_end=day["windowEnd"],
            boundary_policy_revision=None,
            created_by="work-buddy-journal-dashboard",
        )
        frozen_field = next(
            (
                candidate
                for candidate in frozen.fields
                if candidate.composition_slot_id == slot_id
                and candidate.field_id == field_id
                and candidate.field_definition_version == field_version
            ),
            None,
        )
        if frozen_field is None:
            raise JournalCaptureConflict(
                "That Journal day froze with another field layout. Refresh and try again."
            )
        item = JournalNativeSourceService(store, sources).put_field_value(
            source_ref=commit.source_ref,
            representation_id=commit.representation_id,
            service_principal=trusted.service_principal,
            value_id=value_id,
            local_date=local_date,
            module_instance_id=module_id,
            module_instance_version=module_version,
            field_id=field_id,
            field_definition_version=field_version,
            client_mutation_id=mutation_id,
            expected_revision=expected_revision,
            actor={
                "schema": "wb.journal-field-human-actor/v1",
                "actor": trusted.inputter.to_dict(),
            },
            value=body.get("value"),
            disposition=disposition,
            composition_slot_id=slot_id,
            prompt_id=frozen_field.prompt_id,
            prompt_version=frozen_field.prompt_version,
            authorship="human",
            review_state="not_applicable",
            stated_at=stated_at,
        )
        return jsonify(
            {
                "ok": True,
                "deduplicated": commit.deduplicated,
                "fieldValue": field_value_view(item),
            }
        )
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except SourceError as exc:
        error, status = public_error(exc)
        return jsonify({"ok": False, "error": error}), status
    except Exception:
        logger.exception("Journal field edit failed after request validation")
        return _error(
            "journal_field_value_failed",
            "That Journal field could not be saved. Your input is still in the editor.",
            500,
            retryable=True,
        )


@journal_capture_blueprint.post("/api/journal/items/<item_id>/<operation>")
def act_on_journal_item(item_id: str, operation: str):
    """Apply one explicit CAS action to a native Journal item."""

    try:
        if operation not in {
            "edit",
            "correct",
            "resolve",
            "route",
            "tombstone",
            "restore",
        }:
            return _error(
                "journal_item_action_not_found",
                "That Journal item action is unavailable.",
                404,
            )
        body = _body()
        mutation_id, expected_revision = _mutation_fields(body)
        context_sha = _profile_gesture_context(body)
        authority = require_human_authority_request(
            action=f"journal.item.{operation}",
            subject=f"journal-item:{item_id}",
            context_sha256=context_sha,
        )
        sources, store, _service = _services()
        _require_database_authority(store)
        domain = JournalDomainService(store)
        actor = {
            "schema": "wb.journal-http-human-actor/v1",
            "actor": _source_actor(authority.principal.actor).to_dict(),
        }
        relation = None
        deduplicated = False
        if operation in {"edit", "correct"}:
            exact_text = body.get("exactText")
            stated_at = body.get("statedAt")
            if (
                not isinstance(exact_text, str)
                or not exact_text
                or len(exact_text) > 100_000
                or (stated_at is not None and not isinstance(stated_at, str))
            ):
                raise JournalCaptureValidationError(
                    "Enter the Journal text to save."
                )
            trusted = _action_trusted_context(
                authority,
                context_sha256=context_sha,
                namespace="journal-item-action",
                purpose=ITEM_ACTION_PURPOSE,
            )
            commit = TrustedIngressService(sources).commit_human_input(
                trusted,
                HumanInputRequest(
                    exact_content=exact_text,
                    client_mutation_id=f"{mutation_id}:source",
                    input_mode="direct_entry",
                    occurred_at=stated_at,
                ),
            )
            deduplicated = commit.deduplicated
            item = JournalActionSourceService(store, sources).update_item(
                source_ref=commit.source_ref,
                representation_id=commit.representation_id,
                service_principal=trusted.service_principal,
                item_id=item_id,
                expected_revision=expected_revision,
                operation=operation,
                plain_value=exact_text,
                client_mutation_id=mutation_id,
                actor=actor,
            )
        elif operation == "route":
            target_domain = body.get("targetDomain")
            target_id = body.get("targetId")
            target_revision = body.get("targetRevision")
            if (
                not isinstance(target_domain, str)
                or not isinstance(target_id, str)
                or not target_id.strip()
                or (
                    target_revision is not None
                    and not isinstance(target_revision, str)
                )
            ):
                raise JournalCaptureValidationError(
                    "Choose a Journal route destination."
                )
            item, relation = domain.route_native_item(
                item_id=item_id,
                expected_revision=expected_revision,
                target_domain=target_domain,
                target_id=target_id,
                target_revision=target_revision,
                client_mutation_id=mutation_id,
                actor=actor,
            )
        else:
            item = domain.transition_native_item(
                item_id=item_id,
                expected_revision=expected_revision,
                operation=operation,
                client_mutation_id=mutation_id,
                actor=actor,
            )
        payload: dict[str, Any] = {
            "ok": True,
            "deduplicated": deduplicated,
            "item": native_item_view(domain, item),
        }
        if relation is not None:
            payload["relation"] = {
                "relationId": relation.relation_id,
                "relationKind": relation.relation_kind,
                "targetDomain": relation.target_domain,
                "targetId": relation.target_id,
                "targetRevision": relation.target_revision,
                "lifecycle": relation.lifecycle,
                "revision": relation.revision,
            }
        return jsonify(payload)
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except SourceError as exc:
        error, status = public_error(exc)
        return jsonify({"ok": False, "error": error}), status
    except Exception:
        logger.exception("Journal item action failed after request validation")
        return _error(
            "journal_item_action_failed",
            "That Journal item action could not be completed.",
            500,
            retryable=True,
        )


@journal_capture_blueprint.post("/api/journal/prompt-interactions")
def create_journal_prompt_interaction():
    """Freeze exact human seed text separately from any generated variants."""

    try:
        body = _body()
        context_sha = _profile_gesture_context(body)
        mutation_id = body.get("clientMutationId")
        local_date = body.get("localDate")
        module_id = body.get("moduleInstanceId")
        module_version = body.get("moduleInstanceVersion")
        prompt_id = body.get("promptId")
        prompt_version = body.get("promptVersion")
        exact_input = body.get("exactInput")
        stated_at = body.get("statedAt")
        result_retention = body.get("resultRetention", "all_versions")
        result_search_mode = body.get("resultSearchMode", "content")
        if (
            not isinstance(mutation_id, str)
            or not 8 <= len(mutation_id) <= 220
            or not isinstance(local_date, str)
            or not isinstance(module_id, str)
            or not module_id
            or not isinstance(module_version, int)
            or isinstance(module_version, bool)
            or module_version < 1
            or not isinstance(prompt_id, str)
            or not prompt_id
            or not isinstance(prompt_version, int)
            or isinstance(prompt_version, bool)
            or prompt_version < 1
            or not isinstance(exact_input, str)
            or not exact_input
            or len(exact_input) > 100_000
            or (stated_at is not None and not isinstance(stated_at, str))
            or result_retention
            not in {"latest_only", "all_versions", "policy_managed"}
            or result_search_mode not in {"exclude", "metadata_only", "content"}
        ):
            raise JournalCaptureValidationError(
                "That Journal prompt input is invalid."
            )
        try:
            parsed_date = date.fromisoformat(local_date)
        except ValueError as exc:
            raise JournalCaptureValidationError(
                "That Journal day is invalid."
            ) from exc
        if parsed_date.isoformat() != local_date:
            raise JournalCaptureValidationError("That Journal day is invalid.")
        authority = require_human_authority_request(
            action="journal.prompt.create",
            subject=f"journal-prompt:{local_date}:{module_id}:{prompt_id}",
            context_sha256=context_sha,
        )
        sources, store, _service = _services()
        _require_database_authority(store)
        domain = JournalDomainService(store)
        day = current_day(local_date)
        composition = domain.resolve_day(
            local_date=local_date,
            timezone=day["timezone"],
            boundary=day["dayBoundaryStart"],
            window_start=day["windowStart"],
            window_end=day["windowEnd"],
        )
        module = next(
            (
                candidate
                for candidate in composition.modules
                if candidate.semantic_membership == "included"
                and candidate.module.module_instance_id == module_id
                and candidate.module.instance_version == module_version
            ),
            None,
        )
        prompt = next(
            (
                candidate
                for candidate in composition.fields
                if module is not None
                and candidate.module_slot_id == module.slot_id
                and candidate.prompt_id == prompt_id
                and candidate.prompt_version == prompt_version
            ),
            None,
        )
        if module is None or prompt is None:
            raise JournalCaptureConflict(
                "That prompt is no longer part of this Journal day. Refresh and try again."
            )
        trusted = _action_trusted_context(
            authority,
            context_sha256=context_sha,
            namespace="journal-prompt-input",
            purpose=PROMPT_INPUT_PURPOSE,
        )
        commit = TrustedIngressService(sources).commit_human_input(
            trusted,
            HumanInputRequest(
                exact_content=exact_input,
                client_mutation_id=f"{mutation_id}:source",
                input_mode="direct_entry",
                occurred_at=stated_at,
            ),
        )
        frozen = domain.ensure_day(
            local_date=local_date,
            timezone=day["timezone"],
            boundary=day["dayBoundaryStart"],
            window_start=day["windowStart"],
            window_end=day["windowEnd"],
            boundary_policy_revision=None,
            created_by="work-buddy-journal-dashboard",
        )
        interaction_id = "jpi_" + canonical_sha256(
            {
                "schema": "wb.journal-prompt-interaction-id/v1",
                "clientMutationId": mutation_id,
            }
        )[:32]
        interaction = JournalActionSourceService(
            store, sources
        ).create_prompt_interaction(
            source_ref=commit.source_ref,
            representation_id=commit.representation_id,
            service_principal=trusted.service_principal,
            interaction_id=interaction_id,
            local_date=local_date,
            module_instance_id=module_id,
            module_instance_version=module_version,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            input_text=exact_input,
            result_retention=str(result_retention),
            result_search_mode=str(result_search_mode),
            client_mutation_id=mutation_id,
            day_id=day["dayId"],
            composition_snapshot_id=frozen.snapshot_id,
        )
        response = jsonify(
            {
                "ok": True,
                "deduplicated": commit.deduplicated,
                "interaction": interaction,
            }
        )
        response.status_code = 200 if commit.deduplicated else 201
        return response
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except SourceError as exc:
        error, status = public_error(exc)
        return jsonify({"ok": False, "error": error}), status
    except Exception:
        logger.exception("Journal prompt creation failed after request validation")
        return _error(
            "journal_prompt_create_failed",
            "That prompt input could not be saved. Your input is still in the editor.",
            500,
            retryable=True,
        )


@journal_capture_blueprint.post(
    "/api/journal/prompt-interactions/<interaction_id>/generate"
)
def request_journal_prompt_generation(interaction_id: str):
    """Queue a manual generation request; never call a model in this request."""

    try:
        body = _body()
        mutation_id, expected_revision = _mutation_fields(body)
        context_sha = _profile_gesture_context(body)
        authority = require_human_authority_request(
            action="journal.prompt.generate",
            subject=f"journal-prompt:{interaction_id}",
            context_sha256=context_sha,
        )
        _sources, store, _service = _services()
        _require_database_authority(store)
        # Probe the detached execution host before creating durable work. A
        # missing provider is an honest unavailable response, not a queue that
        # can never drain.
        try:
            selection = _prompt_generation_runner.prepare()
        except Exception as exc:
            logger.warning(
                "Journal prompt generation provider is unavailable (%s)",
                getattr(exc, "error_code", type(exc).__name__),
            )
            return _error(
                "journal_prompt_generation_unavailable",
                "No configured background agent is available for generation.",
                503,
                retryable=True,
            )
        domain = JournalDomainService(store)
        interaction = domain.get_prompt_interaction(interaction_id)
        context_manifest = {
            "schema": "wb.journal-prompt-generation-context/v1",
            "interactionId": interaction_id,
            "interactionRevision": expected_revision,
            "input": {
                "sourceRef": interaction["inputSourceRef"],
                "sha256": interaction["inputSha256"],
            },
            "prompt": {
                "promptId": interaction["promptId"],
                "promptVersion": interaction["promptVersion"],
            },
            "disclosedContext": [],
        }
        generation = domain.request_prompt_generation(
            interaction_id=interaction_id,
            expected_revision=expected_revision,
            client_mutation_id=mutation_id,
            actor={
                "schema": "wb.journal-http-human-actor/v1",
                "actor": _source_actor(authority.principal.actor).to_dict(),
            },
            context_manifest=context_manifest,
        )
        dispatch: Mapping[str, Any] | None = None
        if not bool(generation.get("deduplicated")):
            try:
                dispatch = _prompt_generation_runner.start(
                    store=store,
                    request_id=str(generation["requestId"]),
                    selection=selection,
                )
            except Exception as exc:
                logger.warning(
                    "Journal prompt generation worker did not start (%s)",
                    getattr(exc, "code", type(exc).__name__),
                )
                failed = domain.get_prompt_generation_request(
                    str(generation["requestId"])
                )
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": {
                                "code": "journal_prompt_generation_start_failed",
                                "message": (
                                    "The background agent could not start. "
                                    "Choose Generate again to retry."
                                ),
                                "retryable": True,
                            },
                            "generation": failed,
                            "interaction": domain.get_prompt_interaction(
                                interaction_id
                            ),
                        }
                    ),
                    503,
                )
        generation = domain.get_prompt_generation_request(
            str(generation["requestId"])
        )
        generation_status = str(generation["status"])
        message = (
            "A previous launch failed. Choose Generate again to retry."
            if generation_status == "failed"
            else "This generation request already completed."
            if generation_status == "succeeded"
            else "Generation is already in progress."
            if dispatch is None
            else "Generation started. The result will appear when the background agent completes it."
        )
        return jsonify(
            {
                "ok": True,
                "generation": generation,
                "dispatch": dispatch,
                "interaction": domain.get_prompt_interaction(interaction_id),
                "message": message,
            }
        )
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except Exception:
        logger.exception("Journal prompt generation request failed")
        return _error(
            "journal_prompt_generation_failed",
            "Generation could not be queued.",
            500,
            retryable=True,
        )


@journal_capture_blueprint.post(
    "/api/journal/prompt-interactions/<interaction_id>/variants/<variant_id>/decide"
)
def decide_journal_prompt_variant(interaction_id: str, variant_id: str):
    try:
        body = _body()
        mutation_id, expected_revision = _mutation_fields(body)
        decision = body.get("decision")
        if decision not in {"accept", "archive", "reject"}:
            raise JournalCaptureValidationError(
                "Choose a valid prompt result decision."
            )
        context_sha = _profile_gesture_context(body)
        authority = require_human_authority_request(
            action="journal.prompt.decide",
            subject=f"journal-prompt:{interaction_id}:{variant_id}",
            context_sha256=context_sha,
        )
        _sources, store, _service = _services()
        _require_database_authority(store)
        domain = JournalDomainService(store)
        domain.decide_prompt_result(
            interaction_id=interaction_id,
            variant_id=variant_id,
            decision_kind=str(decision),
            expected_revision=expected_revision,
            client_mutation_id=mutation_id,
            actor={
                "schema": "wb.journal-http-human-actor/v1",
                "actor": _source_actor(authority.principal.actor).to_dict(),
            },
        )
        return jsonify(
            {"ok": True, "interaction": domain.get_prompt_interaction(interaction_id)}
        )
    except JournalCaptureError as exc:
        return _error(exc.code, str(exc), 409 if "conflict" in exc.code else 400)
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except Exception:
        logger.exception("Journal prompt decision failed")
        return _error(
            "journal_prompt_decision_failed",
            "That prompt result decision could not be saved.",
            500,
            retryable=True,
        )


@journal_capture_blueprint.post(
    "/api/journal/prompt-generations/<request_id>/results"
)
def ingest_journal_prompt_result(request_id: str):
    """Consume an identified agent-output Source under a generation lease."""

    try:
        body = _body()
        lease_token = body.get("leaseToken")
        source_ref_raw = body.get("sourceRef")
        representation_id = body.get("representationId")
        mutation_id = body.get("clientMutationId")
        producer_id = body.get("producerId")
        provider_id = body.get("providerId")
        model_id = body.get("modelId")
        receipt = body.get("generationReceipt")
        if (
            not isinstance(lease_token, str)
            or len(lease_token) < 32
            or not isinstance(source_ref_raw, str)
            or not isinstance(representation_id, str)
            or not representation_id
            or not isinstance(mutation_id, str)
            or not 8 <= len(mutation_id) <= 220
            or not isinstance(producer_id, str)
            or not producer_id
            or (provider_id is not None and not isinstance(provider_id, str))
            or (model_id is not None and not isinstance(model_id, str))
            or not isinstance(receipt, Mapping)
        ):
            raise JournalCaptureValidationError(
                "That prompt result receipt is invalid."
            )
        source_ref = SourceRef.parse(source_ref_raw)
        sources, store, _service = _services()
        _require_database_authority(store)
        domain = JournalDomainService(store)
        generation = domain.validate_prompt_generation_lease(
            request_id=request_id,
            lease_token=lease_token,
        )
        source_item = sources.get_item(source_ref)
        if source_item is None or source_item.source_role != "agent_output":
            raise JournalCaptureValidationError(
                "Prompt results must reference identified agent output."
            )
        principal = journal_service_principal(
            sources,
            source_ref,
            purpose=PROMPT_RESULT_PURPOSE,
        )
        resolved = resolve_source(
            sources,
            source_ref=source_ref,
            representation_id=representation_id,
            principal=principal,
            purpose=PROMPT_RESULT_PURPOSE,
        )
        try:
            result_text = resolved.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JournalCaptureValidationError(
                "Prompt result output must be UTF-8 text."
            ) from exc
        if not result_text or len(result_text) > 200_000:
            raise JournalCaptureValidationError(
                "Prompt result output is empty or too large."
            )
        variant_id = JournalActionSourceService(
            store, sources
        ).record_prompt_result(
            source_ref=source_ref,
            representation_id=representation_id,
            service_principal=principal,
            interaction_id=str(generation["interactionId"]),
            expected_revision=int(generation["interactionRevision"]),
            client_mutation_id=mutation_id,
            producer_id=producer_id,
            context_manifest_sha256=str(generation["contextManifestSha256"]),
            generation_receipt=dict(receipt),
            result_text=result_text,
            generation_request_id=request_id,
            lease_token=lease_token,
            provider_id=provider_id,
            model_id=model_id,
        )
        return jsonify(
            {
                "ok": True,
                "variantId": variant_id,
                "interaction": domain.get_prompt_interaction(
                    str(generation["interactionId"])
                ),
            }
        )
    except (ValueError, JournalCaptureError) as exc:
        code = exc.code if isinstance(exc, JournalCaptureError) else "journal_capture_invalid"
        return _error(code, str(exc), 409 if "conflict" in code else 400)
    except SourceError as exc:
        error, status = public_error(exc)
        return jsonify({"ok": False, "error": error}), status
    except Exception:
        logger.exception("Journal prompt result ingestion failed")
        return _error(
            "journal_prompt_result_failed",
            "That generated result could not be retained.",
            500,
            retryable=True,
        )


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
        disclosure_sha = body.get("smart_disclosure_sha256")
        if disclosure_sha is not None and (not isinstance(disclosure_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", disclosure_sha)):
            raise JournalCaptureValidationError("The Smart disclosure is invalid.")
        stated_at = body.get("stated_at")
        if stated_at is not None and not isinstance(stated_at, str):
            raise JournalCaptureValidationError("The capture time is invalid.")
        trusted = _trusted_context(authority, mode=mode, context_sha256=context_sha)
        sources, store, service = _services()
        follow_up_action = body.get("follow_up_action")
        if follow_up_action is not None and (
            follow_up_action != "task_proposal" or mode is not CaptureMode.DUMB
            or target is not CaptureTarget.RUNNING_NOTES
        ):
            raise JournalCaptureValidationError("Save and propose task uses Running Notes without a model.")
        ingress = JournalCaptureIngress(
            sources,
            service,
            service_principal=_service_principal(authority),
            worker_id="journal-http",
        ).submit(
            trusted=trusted,
            exact_text=exact_text,
            client_mutation_id=client_mutation_id,
            day_id=day_id,
            target=target,
            mode=mode,
            input_mode=input_mode,
            stated_at=stated_at,
            authorization_expires_at=_authorization_expires_at(authority),
            follow_up_action=follow_up_action,
            smart_disclosure_sha256=disclosure_sha,
        )
        commit = ingress.commit
        capture = ingress.capture
        capture_id = capture.capture_id
        response = jsonify(
            {
                "ok": True,
                "persisted": True,
                "deduplicated": commit.deduplicated,
                "capture": capture_view(store, capture, follow_ups=service.proposal_follow_ups(capture_id)),
            }
        )
        response.status_code = 200 if commit.deduplicated else 201
        return response
    except JournalIngressQueued as exc:
        response = jsonify(
            {
                "ok": True,
                "persisted": True,
                "queued": True,
                "deduplicated": exc.commit.deduplicated,
                "capture": None,
                "message": str(exc),
            }
        )
        response.status_code = 202
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
        body = _body()
        sources, store, service = _services()
        capture = store.get_capture(capture_id)
        if capture is None:
            return _error("journal_capture_not_found", "That capture is unavailable.", 404)
        disclosure_sha = body.get("smart_disclosure_sha256")
        context = f"wb.journal-capture-retry/v1:{capture_id}:{capture.revision}"
        if disclosure_sha is not None:
            if not isinstance(disclosure_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", disclosure_sha):
                raise JournalCaptureValidationError("The Smart disclosure is invalid.")
            context += ":" + disclosure_sha
        context_sha = hashlib.sha256(context.encode()).hexdigest()
        authority = require_human_authority_request(
            action="journal.capture.retry",
            subject=f"journal-capture:{capture_id}",
            context_sha256=context_sha,
        )
        # Fail before resolving retained bytes, reauthorizing an effect, or
        # invoking a model/follow-up while cutover maintenance is held.
        service.authority.capture_mode()
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
            service.refresh_smart_availability()
            if disclosure_sha is not None:
                store.bind_smart_disclosure(capture_id, disclosure_sha, retry=True)
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
        for effect in store.effects_for_capture(capture_id):
            if effect.effect_type == "task_proposal" and effect.state.value != "succeeded":
                retry_context = _trusted_context(authority, mode=capture.mode, context_sha256=context_sha)
                store.reauthorize_effect(capture_id, "task_proposal",
                    authorization_fingerprint=retry_context.authorization_fingerprint,
                    authorization_expires_at=_authorization_expires_at(authority))
                service.deliver_proposal(capture_id)
        return jsonify({"ok": True, "capture": capture_view(store, updated, follow_ups=service.proposal_follow_ups(capture_id))})
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
    "/api/journal/document-modules/<local_date>/<module_instance_id>/open"
)
def open_journal_document_module(local_date: str, module_instance_id: str):
    """Provision a day-scoped Journal working document on explicit user intent."""

    try:
        body = _body()
        mutation_id = body.get("clientMutationId")
        module_version = body.get("moduleInstanceVersion")
        if (
            not isinstance(mutation_id, str)
            or not 8 <= len(mutation_id) <= 220
            or not isinstance(module_version, int)
            or isinstance(module_version, bool)
            or module_version < 1
            or not module_instance_id
        ):
            raise JournalCaptureValidationError(
                "That Journal document request is invalid."
            )
        try:
            parsed_date = date.fromisoformat(local_date)
        except ValueError as exc:
            raise JournalCaptureValidationError(
                "That Journal day is invalid."
            ) from exc
        if parsed_date.isoformat() != local_date:
            raise JournalCaptureValidationError("That Journal day is invalid.")
        authority = require_human_authority_request(
            action="journal.document.open",
            subject=f"journal-document:{local_date}:{module_instance_id}",
            context_sha256=_profile_gesture_context(body),
        )
        _sources, store, service = _services()
        _require_database_authority(store)
        day = current_day(local_date)
        domain = JournalDomainService(store)
        composition = domain.resolve_day(
            local_date=local_date,
            timezone=day["timezone"],
            boundary=day["dayBoundaryStart"],
            window_start=day["windowStart"],
            window_end=day["windowEnd"],
        )
        module = next(
            (
                candidate
                for candidate in composition.modules
                if candidate.semantic_membership == "included"
                and candidate.module.module_instance_id == module_instance_id
                and candidate.module.instance_version == module_version
                and candidate.module.module_type_id == "document"
                and candidate.module.behavior_id == "provenance_only"
            ),
            None,
        )
        if module is None:
            raise JournalCaptureConflict(
                "That document section is no longer part of this Journal day. "
                "Refresh and try again."
            )
        settings = module.module.settings
        role = str(settings.get("documentRole") or "journal_document")
        if (
            settings.get("truthEligibility", "allowed") != "allowed"
            or settings.get("initialTruthActivation", "disabled") != "disabled"
        ):
            raise JournalCaptureConflict(
                "That Journal document policy is unavailable. Refresh and try again."
            )
        composition = domain.ensure_day(
            local_date=local_date,
            timezone=day["timezone"],
            boundary=day["dayBoundaryStart"],
            window_start=day["windowStart"],
            window_end=day["windowEnd"],
            boundary_policy_revision=None,
            created_by="work-buddy-journal-dashboard",
        )
        module = next(
            (
                candidate
                for candidate in composition.modules
                if candidate.semantic_membership == "included"
                and candidate.module.module_instance_id == module_instance_id
                and candidate.module.instance_version == module_version
                and candidate.module.module_type_id == "document"
                and candidate.module.behavior_id == "provenance_only"
            ),
            None,
        )
        if module is None:
            raise JournalCaptureConflict(
                "That document section is no longer part of this Journal day. "
                "Refresh and try again."
            )
        existing = store.get_module_document_binding(
            local_date=local_date,
            module_instance_id=module_instance_id,
            module_instance_version=module_version,
        )
        if existing is not None:
            return jsonify(
                {
                    "ok": True,
                    "deduplicated": True,
                    "document": {
                        "state": "current",
                        "role": existing.role,
                        "truthEligibility": "allowed",
                        "truthStartsDisabled": True,
                        "href": existing.cowork_href,
                        "storeId": existing.store_id,
                        "documentId": existing.document_id,
                        "bindingId": existing.binding_id,
                        "domainEntityId": existing.domain_entity_id,
                        "contentAuthorityEpoch": existing.content_authority_epoch,
                        "canOpenFull": True,
                    },
                }
            )
        entity_id = hashlib.sha256(
            "\0".join(
                (
                    "journal-document-module/v1",
                    local_date,
                    module_instance_id,
                    str(module_version),
                    role,
                )
            ).encode("utf-8")
        ).hexdigest()[:32]
        actor = json.dumps(
            _source_actor(authority.principal.actor).to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = RunningNoteDocumentService(kernel=_kernel()).provision_empty(
            vault_root=service.adapter.vault_root,
            entity_id=entity_id,
            domain_revision=(
                f"journal-composition:{composition.composition_digest}:"
                f"module:{module_instance_id}:{module_version}"
            ),
            role=role,
            title=module.module.label,
            created_by=actor,
        )
        binding = result.binding
        stored = store.record_module_document_binding(
            local_date=local_date,
            module_instance_id=module_instance_id,
            module_instance_version=module_version,
            domain_entity_id=entity_id,
            binding_id=binding.binding_id,
            store_id=binding.store_id,
            document_id=binding.document_id,
            role=binding.role,
            cowork_href=result.cowork_href,
            content_authority_epoch=binding.content_authority_epoch,
        )
        return jsonify(
            {
                "ok": True,
                "deduplicated": False,
                "document": {
                    "state": "current",
                    "role": stored.role,
                    "truthEligibility": "allowed",
                    "truthStartsDisabled": True,
                    "href": stored.cowork_href,
                    "storeId": stored.store_id,
                    "documentId": stored.document_id,
                    "bindingId": stored.binding_id,
                    "domainEntityId": stored.domain_entity_id,
                    "contentAuthorityEpoch": stored.content_authority_epoch,
                    "canOpenFull": True,
                },
            }
        ), 201
    except JournalCaptureError as exc:
        return _error(
            exc.code,
            str(exc),
            409 if exc.retryable or isinstance(exc, JournalCaptureConflict) else 400,
            retryable=exc.retryable,
        )
    except LocalIdentityError as exc:
        return _error(exc.code, str(exc), exc.status, retryable=False)
    except Exception:
        logger.exception("Journal document module provisioning failed")
        return _error(
            "journal_document_open_failed",
            "Co-work could not open that Journal document.",
            500,
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
