"""Content-free JSON projections for the Journal HTTP provider."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable

from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import JournalCapture, JournalEffect, JournalEntry, JournalSmartAvailability
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.settings import peek_journal_day_binding, peek_journal_day_window


def current_day(local_date: str | None = None) -> dict[str, Any]:
    """Resolve the day a Journal view is about, current or explicitly named.

    Both branches read the same policy history and supply exactly the keys
    projected below, so a named day is described by the policy that was in
    force for it.  The current-day binding also carries the revision of the
    boundary setting, which a named day deliberately does not: that revision
    describes the value in force now, so attributing it to an earlier day would
    misdate the policy that day actually ran under.  A day's policy identity
    travels instead as its timezone, boundary, and window bounds.
    """

    if local_date is None:
        binding, _event = peek_journal_day_binding()
    else:
        window = peek_journal_day_window(local_date)
        binding = {
            "local_date": window.local_date.isoformat(),
            "timezone": window.timezone,
            "day_boundary_start": window.boundary,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
        }
    return {
        "dayId": (
            f"journal-day:{binding['local_date']}:{binding['timezone']}:"
            f"{binding['day_boundary_start']}"
        ),
        "localDate": binding["local_date"],
        "timezone": binding["timezone"],
        "dayBoundaryStart": binding["day_boundary_start"],
        "windowStart": binding["window_start"],
        "windowEnd": binding["window_end"],
        "now": datetime.now(UTC).isoformat(),
    }


def capture_view(store: JournalCaptureStore, capture: JournalCapture, *,
                 follow_ups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    effects = store.effects_for_capture(capture.capture_id)
    materialize = next((e for e in effects if e.effect_type == "materialize"), None)
    placement = _placement_status(materialize, capture)
    error_message = None
    if placement == "failed":
        error_message = "Saved, but Journal placement needs another try."
    elif capture.processing_status.value == "failed":
        error_message = "Saved, but optional smart processing did not finish."
    proposal = next((e for e in effects if e.effect_type == "task_proposal"), None)
    proposal_canceled = proposal is not None and proposal.error_code == "journal_proposal_source_withdrawn"
    if proposal is not None and proposal.state.value in {"failed", "paused"}:
        error_message = ("Source removed; the unsent task proposal was canceled." if proposal_canceled else
                         "Saved, but the task proposal needs another try. No task was created.")
    entry = store.get_entry(capture.entry_id) if capture.entry_id is not None else None
    return {
        "captureId": capture.capture_id,
        "clientMutationId": capture.client_mutation_id,
        "targetId": capture.requested_target.value,
        "resolvedTargetId": (
            capture.resolved_target.value if capture.resolved_target is not None else None
        ),
        "mode": capture.mode.value,
        "exactText": entry.markdown if entry is not None else None,
        "submittedAt": capture.submitted_at,
        "persistenceStatus": capture.persistence_status,
        "placementStatus": placement,
        "processingStatus": capture.processing_status.value,
        "annotation": dict(capture.annotation) if capture.annotation else None,
        "errorMessage": error_message,
        "entryId": capture.entry_id,
        "revision": capture.revision,
        "sourceRef": capture.source_ref,
        "followUps": follow_ups or [],
        "retryable": error_message is not None and not proposal_canceled,
    }


def running_note_document_gesture_context(entry: JournalEntry) -> str:
    """Bind the visible Open-in-Co-work action to the exact Journal revision."""

    return hashlib.sha256(
        json.dumps(
            {
                "content_sha256": entry.content_sha256,
                "entry_id": entry.entry_id,
                "entry_version": entry.version,
                "schema": "wb.journal-running-note-cowork-gesture/v1",
                "source_ref": entry.source_ref,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def view_snapshot(
    store: JournalCaptureStore,
    *,
    smart_processing_available: bool = False,
    smart_processing_disclosure: str | None = None,
    smart_availability: JournalSmartAvailability | None = None,
    proposal_follow_ups: Callable[[str], list[dict[str, Any]]] | None = None,
    local_date: str | None = None,
) -> dict[str, Any]:
    day = current_day() if local_date is None else current_day(local_date)
    domain = JournalDomainService(store)
    composition = domain.resolve_day(
        local_date=day["localDate"],
        timezone=day["timezone"],
        boundary=day["dayBoundaryStart"],
        window_start=day["windowStart"],
        window_end=day["windowEnd"],
    )
    captures = store.list_captures(day["localDate"], limit=20)
    follow_ups = {item.capture_id: proposal_follow_ups(item.capture_id) if proposal_follow_ups else [] for item in captures}
    notes = store.list_running_notes(day["localDate"])
    if proposal_follow_ups is not None:
        for note in notes:
            if note.capture_id not in follow_ups:
                follow_ups[note.capture_id] = proposal_follow_ups(note.capture_id)
    logs = store.list_log_entries(day["localDate"])
    all_native_items = domain.list_native_items(
        day["localDate"], include_inactive=True
    )
    native_items = tuple(
        item
        for item in all_native_items
        if item.lifecycle not in {"tombstoned", "superseded"}
    )
    native_records = [
        item
        for item in native_items
        if item.authority_kind != "legacy_entry" and item.item_kind == "record"
    ]
    native_notes = [
        item
        for item in native_items
        if item.authority_kind != "legacy_entry" and item.item_kind == "running_note"
    ]
    field_values = domain.list_field_values(day["localDate"])
    authority_state = domain.authority_state()
    prompt_interactions = (
        domain.list_prompt_interactions(day["localDate"], include_tombstoned=True)
        if authority_state == "database_only"
        else ()
    )
    availability = smart_availability or (
        JournalSmartAvailability(state="ready", code="ready", reason="Smart is ready.")
        if smart_processing_available else JournalSmartAvailability()
    )
    revision_payload = {
        "captures": [(item.capture_id, item.revision, item.updated_at) for item in captures],
        "notes": [(item.entry_id, item.version, item.updated_at) for item in notes],
        "logs": [(item.entry_id, item.version, item.updated_at) for item in logs],
        "documents": [
            (
                item.entry_id,
                binding.binding_id if binding is not None else None,
                binding.change_id if binding is not None else None,
                binding.state if binding is not None else None,
                binding.updated_at if binding is not None else None,
            )
            for item in notes
            for binding in (store.get_document_binding(item.entry_id),)
        ],
        "day": day["dayId"],
        "followUps": follow_ups,
        "smartAvailability": availability.as_dict(),
        "composition": composition.composition_digest,
        "nativeItems": [
            (item.item_id, item.current_revision, item.updated_at)
            for item in all_native_items
        ],
        "fieldValues": [
            (item.value_id, item.current_revision, item.ingested_at) for item in field_values
        ],
        "promptInteractions": [
            (
                item["interactionId"],
                item["currentRevision"],
                item["updatedAt"],
                [
                    (request["requestId"], request["status"], request["updatedAt"])
                    for request in item["generationRequests"]
                ],
            )
            for item in prompt_interactions
        ],
    }
    revision = "journal:" + hashlib.sha256(
        json.dumps(revision_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:16]
    smart_processing_available = availability.state == "ready"
    availability_view = availability.as_dict()
    if availability.state == "disabled_by_policy":
        availability_view["action"] = {"kind": "app_link", "label": "Set up Smart",
            "href": "/app/settings/apps/journal?setting=wb.journal.smart-processing"}
    elif availability.state == "provider_unavailable":
        availability_view["action"] = {"kind": "retry", "label": "Retry Smart setup"}
    return {
        "schemaVersion": 1,
        "revision": revision,
        "observedAt": datetime.now(UTC).isoformat(),
        "day": day,
        "access": {"mode": "read_write"},
        "quality": {"freshness": "current", "observedAt": datetime.now(UTC).isoformat(), "issues": []},
        "source": {"kind": "live"},
        "capture": {
            "instanceId": "default:capture",
            "revision": revision,
            "dayId": day["dayId"],
            "access": {"mode": "read_write"},
            "smartAvailability": availability_view,
            "secondaryActions": [{"actionId": "task_proposal", "label": "Save and propose task",
                "description": "Saves exact text in Running Notes and a task proposal for review. No model runs and no task is created.",
                "targetId": "running_notes", "mode": "dumb"}],
            "smartHelp": (
                None
                if smart_processing_disclosure is None
                else {
                    "summary": "Smart classifies the saved capture with the configured model.",
                    "details": smart_processing_disclosure,
                }
            ),
            "targets": [
                {
                    "targetId": "auto",
                    "label": "Auto",
                    "description": (
                        smart_processing_disclosure
                        or "Journal uses optional smart processing to choose one destination."
                    ),
                    "supportedModes": ["smart"],
                    "defaultMode": "smart",
                    "enabled": smart_processing_available,
                    "unavailableReason": (
                        None
                        if smart_processing_available
                        else availability.reason
                    ),
                },
                {
                    "targetId": "log",
                    "label": "Log",
                    "description": "Record it in today's chronological Log.",
                    "supportedModes": (
                        ["dumb", "smart"]
                        if smart_processing_available
                        else ["dumb"]
                    ),
                    "defaultMode": "dumb",
                    "enabled": True,
                },
                {
                    "targetId": "running_notes",
                    "label": "Running Notes",
                    "description": "Keep it as an actionable Running Note.",
                    "supportedModes": (
                        ["dumb", "smart"]
                        if smart_processing_available
                        else ["dumb"]
                    ),
                    "defaultMode": "dumb",
                    "enabled": True,
                },
            ],
            "capturesToday": len(captures),
            "recentSubmissions": [capture_view(store, item, follow_ups=follow_ups[item.capture_id]) for item in captures],
        },
        "runningNotes": {
            "instanceId": "default:running-notes",
            "revision": revision,
            "dayId": day["dayId"],
            "access": (
                {"mode": "read_write"}
                if authority_state == "database_only"
                else {
                    "mode": "read_only",
                    "reason": "Open a running note in Co-work to edit it.",
                }
            ),
            "displayMode": "chronological",
            "items": [
                *[
                    _running_note(
                        store,
                        item,
                        follow_ups=follow_ups.get(item.capture_id, []),
                    )
                    for item in notes
                ],
                *[_native_note(item) for item in native_notes],
            ],
            "legacyCompatibility": {
                "status": "unmanaged_present_or_unknown",
                "message": "Older notes without stable IDs are shown read-only.",
            },
            "tombstones": [
                _native_note(item)
                for item in all_native_items
                if item.authority_kind != "legacy_entry"
                and item.item_kind == "running_note"
                and item.lifecycle == "tombstoned"
            ],
        },
        "logEntries": [
            *[_log_entry(item) for item in logs],
            *[_native_log_entry(item) for item in native_records],
        ],
        "fieldValues": [field_value_view(item) for item in field_values],
        "nativeItems": [
            native_item_view(domain, item)
            for item in all_native_items
            if item.authority_kind != "legacy_entry"
        ],
        "promptInteractions": list(prompt_interactions),
        "effectiveComposition": _effective_composition(
            store,
            composition,
            authority_state=authority_state,
        ),
    }


def _placement_status(effect: JournalEffect | None, capture: JournalCapture) -> str:
    if capture.requested_target.value == "auto" and capture.resolved_target is None:
        return "pending"
    if effect is None:
        return "pending"
    return {
        "pending": "pending",
        "running": "pending",
        "succeeded": "placed",
        "failed": "failed",
        "paused": "failed",
    }[effect.state.value]


def _running_note(store: JournalCaptureStore, entry: JournalEntry, *,
                  follow_ups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    binding = store.get_document_binding(entry.entry_id)
    document: dict[str, Any]
    if binding is None:
        document = {
            "state": "available",
            "gestureContextSha256": running_note_document_gesture_context(entry),
        }
    else:
        document = {
            "state": binding.state,
            "href": binding.cowork_href,
            "storeId": binding.store_id,
            "documentId": binding.document_id,
            "changeId": binding.change_id,
            "contentAuthorityEpoch": binding.content_authority_epoch,
            "gestureContextSha256": running_note_document_gesture_context(entry),
        }
    return {
        "itemId": entry.entry_id,
        "markdown": entry.markdown,
        "createdAt": entry.created_at,
        "updatedAt": entry.updated_at,
        "provenance": {
            "source": "local_submission",
            "label": "Submitted from the local profile; authorship not determined",
        },
        "captureMode": (
            "smart" if entry.processing_status.value != "not_requested" else "dumb"
        ),
        "processing": {
            "state": entry.processing_status.value,
            "annotation": dict(entry.annotation) if entry.annotation else None,
            "errorMessage": (
                "Optional smart processing did not finish."
                if entry.processing_status.value == "failed"
                else None
            ),
        },
        "resolutionState": entry.resolution_state,
        "version": entry.version,
        "sourceRef": entry.source_ref,
        "projectionState": entry.projection_state.value,
        "document": document,
        "followUps": follow_ups or [],
    }


def _log_entry(entry: JournalEntry) -> dict[str, Any]:
    return {
        "itemId": entry.entry_id,
        "itemKind": "record",
        "markdown": entry.markdown,
        "text": entry.markdown,
        "createdAt": entry.created_at,
        "updatedAt": entry.updated_at,
        "revision": entry.version,
        "lifecycle": "current" if entry.resolution_state == "open" else "resolved",
        "authorityKind": "legacy_entry",
        "sourceRef": entry.source_ref,
        "moduleInstanceId": None,
        "moduleInstanceVersion": None,
        "projectionState": entry.projection_state.value,
    }


def _native_log_entry(item) -> dict[str, Any]:
    return {
        "itemId": item.item_id,
        "itemKind": item.item_kind,
        "markdown": item.plain_value,
        "text": item.plain_value,
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
        "revision": item.current_revision,
        "lifecycle": item.lifecycle,
        "authorityKind": item.authority_kind,
        "sourceRef": item.source_ref,
        "moduleInstanceId": item.module_instance_id,
        "moduleInstanceVersion": item.module_instance_version,
    }


def _native_note(item) -> dict[str, Any]:
    return {
        "itemId": item.item_id,
        "markdown": item.plain_value,
        "text": item.plain_value,
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
        "provenance": {
            "source": "local_submission",
            "label": "Stored from retained Source ingress",
        },
        "captureMode": "dumb",
        "processing": {"state": "not_requested", "annotation": None, "errorMessage": None},
        "resolutionState": {
            "current": "open",
            "resolved": "dismissed",
            "archived": "dismissed",
            "tombstoned": "dismissed",
            "superseded": "dismissed",
        }.get(item.lifecycle, "open"),
        "version": item.current_revision,
        "sourceRef": item.source_ref,
        "authorityKind": item.authority_kind,
        "moduleInstanceId": item.module_instance_id,
        "moduleInstanceVersion": item.module_instance_version,
        "followUps": [],
    }


def _native_module_item(item) -> dict[str, Any]:
    return {
        "itemId": item.item_id,
        "itemKind": item.item_kind,
        "text": item.plain_value or "",
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
        "revision": item.current_revision,
        "lifecycle": item.lifecycle,
        "authorityKind": item.authority_kind,
        "sourceRef": item.source_ref,
        "moduleInstanceId": item.module_instance_id,
        "moduleInstanceVersion": item.module_instance_version,
    }


def native_item_view(domain: JournalDomainService, item) -> dict[str, Any]:
    """Project one versioned native item with its public action state."""

    relations = [
        {
            "relationId": relation.relation_id,
            "relationKind": relation.relation_kind,
            "targetDomain": relation.target_domain,
            "targetId": relation.target_id,
            "targetRevision": relation.target_revision,
            "lifecycle": relation.lifecycle,
            "revision": relation.revision,
            "createdAt": relation.created_at,
            "updatedAt": relation.updated_at,
        }
        for relation in domain.list_relations(item.item_id)
    ]
    actions: list[str] = []
    if item.authority_kind == "native_plain":
        if item.lifecycle not in {"tombstoned", "superseded"}:
            actions.extend(("edit", "correct"))
    if item.authority_kind in {"native_plain", "generated"}:
        if item.lifecycle == "current":
            actions.extend(("resolve", "route", "tombstone"))
        elif item.lifecycle == "resolved":
            actions.extend(("route", "restore", "tombstone"))
        elif item.lifecycle == "archived":
            actions.extend(("restore", "tombstone"))
        elif item.lifecycle == "tombstoned":
            actions.append("restore")
    return {
        **_native_module_item(item),
        "actions": actions,
        "relations": relations,
    }


def field_value_view(item) -> dict[str, Any]:
    """Project one typed field value for HTTP/provider boundaries."""

    return {
        "valueId": item.value_id,
        "localDate": item.local_date,
        "dayId": item.day_id,
        "compositionSnapshotId": item.composition_snapshot_id,
        "compositionSlotId": item.composition_slot_id,
        "moduleInstanceId": item.module_instance_id,
        "moduleInstanceVersion": item.module_instance_version,
        "fieldId": item.field_id,
        "fieldDefinitionVersion": item.field_definition_version,
        "valueKind": item.value_kind.value,
        "disposition": item.disposition.value if item.disposition is not None else None,
        "value": item.value,
        "currentRevision": item.current_revision,
        "authorship": item.authorship,
        "reviewState": item.review_state,
        "sourceRef": item.source_ref,
        "observedAt": item.observed_at,
        "statedAt": item.stated_at,
        "ingestedAt": item.ingested_at,
        "lifecycle": item.lifecycle,
    }


def _effective_composition(
    store: JournalCaptureStore,
    composition,
    *,
    authority_state: str,
) -> dict[str, Any]:
    with store._connect() as conn:
        behavior_definitions = {
            (str(row["behavior_id"]), int(row["behavior_version"])): json.loads(
                str(row["definition_json"])
            )
            for row in conn.execute(
                "SELECT behavior_id,behavior_version,definition_json "
                "FROM journal_interaction_behavior_revisions"
            ).fetchall()
        }
    fields_by_module: dict[str, list[dict[str, Any]]] = {}
    for field in composition.fields:
        fields_by_module.setdefault(field.module_slot_id, []).append(
            {
                "compositionSlotId": field.composition_slot_id,
                "ordinal": field.ordinal,
                "fieldId": field.field_id,
                "fieldDefinitionVersion": field.field_definition_version,
                "label": field.label,
                "description": field.description,
                "valueKind": field.value_kind,
                "unit": field.unit,
                "constraints": dict(field.constraints),
                "valueCodecVersion": field.value_codec_version,
                "functionId": field.function_id,
                "functionVersion": field.function_version,
                "behaviorId": field.behavior_id,
                "behaviorVersion": field.behavior_version,
                "privacyClass": field.privacy_class,
                "searchMode": field.search_mode,
                "disclosurePolicyId": field.disclosure_policy_id,
                "promptId": field.prompt_id,
                "promptVersion": field.prompt_version,
                "promptWording": field.prompt_wording,
                "promptHelp": field.prompt_help,
                "promptRequiredness": field.prompt_requiredness,
            }
        )
    modules: list[dict[str, Any]] = []
    for item in composition.modules:
        projected = {
            "slotId": item.slot_id,
            "ordinal": item.ordinal,
            "moduleInstanceId": item.module.module_instance_id,
            "moduleInstanceVersion": item.module.instance_version,
            "moduleTypeId": item.module.module_type_id,
            "moduleTypeVersion": item.module.module_type_version,
            "label": item.module.label,
            "behaviorId": item.module.behavior_id,
            "behaviorVersion": item.module.behavior_version,
            "aiContribution": behavior_definitions.get(
                (item.module.behavior_id, item.module.behavior_version), {}
            ).get("aiContribution", "forbidden"),
            "semanticMembership": item.semantic_membership,
            "settings": dict(item.module.settings),
            "scheduleKind": item.module.schedule_kind,
            "scheduleEvidence": dict(item.schedule_evidence),
            "fields": fields_by_module.get(item.slot_id, []),
        }
        if item.module.module_type_id == "document":
            binding = store.get_module_document_binding(
                local_date=composition.local_date,
                module_instance_id=item.module.module_instance_id,
                module_instance_version=item.module.instance_version,
            )
            role = str(item.module.settings.get("documentRole") or "journal_document")
            projected["document"] = (
                {
                    "state": "available",
                    "role": role,
                    "truthEligibility": "allowed",
                    "truthStartsDisabled": True,
                }
                if binding is None
                else {
                    "state": "current",
                    "role": binding.role,
                    "truthEligibility": "allowed",
                    "truthStartsDisabled": True,
                    "href": binding.cowork_href,
                    "storeId": binding.store_id,
                    "documentId": binding.document_id,
                    "bindingId": binding.binding_id,
                    "domainEntityId": binding.domain_entity_id,
                    "contentAuthorityEpoch": binding.content_authority_epoch,
                    "canOpenFull": True,
                }
            )
        modules.append(projected)
    return {
        "schemaVersion": 1,
        "persisted": composition.persisted,
        "snapshotId": composition.snapshot_id,
        "snapshotVersion": composition.snapshot_version,
        "compositionDigest": composition.composition_digest,
        "searchRecipeVersion": composition.search_recipe_version,
        "activationRevision": composition.activation_revision,
        "authorityState": authority_state,
        "profile": {
            "profileId": composition.profile.profile_id,
            "profileRevision": composition.profile.profile_revision,
            "formatVersion": composition.profile.format_version,
            "name": composition.profile.name,
            "description": composition.profile.description,
            "profileDigest": composition.profile.profile_digest,
        },
        "modules": modules,
    }
