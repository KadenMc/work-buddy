"""Content-free JSON projections for the Journal HTTP provider."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from work_buddy.journal_capture.models import JournalCapture, JournalEffect, JournalEntry
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.settings import get_journal_day_binding


def current_day() -> dict[str, Any]:
    binding, _event = get_journal_day_binding()
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


def capture_view(store: JournalCaptureStore, capture: JournalCapture) -> dict[str, Any]:
    effects = store.effects_for_capture(capture.capture_id)
    materialize = next((e for e in effects if e.effect_type == "materialize"), None)
    placement = _placement_status(materialize, capture)
    error_message = None
    if placement == "failed":
        error_message = "Saved, but Journal placement needs another try."
    elif capture.processing_status.value == "failed":
        error_message = "Saved, but optional smart processing did not finish."
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
) -> dict[str, Any]:
    day = current_day()
    captures = store.list_captures(day["localDate"], limit=20)
    notes = store.list_running_notes(day["localDate"])
    logs = store.list_log_entries(day["localDate"])
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
    }
    revision = "journal:" + hashlib.sha256(
        json.dumps(revision_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:16]
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
                    "label": "Let Journal route it",
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
                        else "Automatic routing is not configured yet."
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
            "recentSubmissions": [capture_view(store, item) for item in captures],
        },
        "runningNotes": {
            "instanceId": "default:running-notes",
            "revision": revision,
            "dayId": day["dayId"],
            "access": {
                "mode": "read_only",
                "reason": (
                    "Captured notes are observable here; editing remains in the "
                    "authoritative daily note until document authority is migrated."
                ),
            },
            "displayMode": "chronological",
            "items": [_running_note(store, item) for item in notes],
            "legacyCompatibility": {
                "status": "unmanaged_present_or_unknown",
                "message": "Older unmarked notes stay read-only until their identities are migrated.",
            },
        },
        "logEntries": [_log_entry(item) for item in logs],
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


def _running_note(store: JournalCaptureStore, entry: JournalEntry) -> dict[str, Any]:
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
    }


def _log_entry(entry: JournalEntry) -> dict[str, Any]:
    return {
        "itemId": entry.entry_id,
        "markdown": entry.markdown,
        "createdAt": entry.created_at,
        "sourceRef": entry.source_ref,
        "projectionState": entry.projection_state.value,
    }
