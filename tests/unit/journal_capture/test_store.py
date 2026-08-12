from __future__ import annotations

import hashlib

import pytest

from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCaptureConflict,
    ProcessingState,
)
from work_buddy.journal_capture.store import JournalCaptureStore


def _capture(store: JournalCaptureStore, *, mutation: str = "mutation-1"):
    return store.create_capture(
        client_mutation_id=mutation,
        request_sha256=hashlib.sha256(mutation.encode()).hexdigest(),
        source_ref="wb-source://authority/item-1",
        representation_id="representation-1",
        submission_id=f"submission-{mutation}",
        command_id=f"command-{mutation}",
        source_effect_id=f"effect-{mutation}",
        day_id="2026-08-09",
        requested_target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB,
        input_mode="direct_entry",
        stated_at=None,
        submitted_at="2026-08-09T21:00:00+00:00",
        authorization_fingerprint="auth-fingerprint",
    )


def test_capture_is_idempotent_and_payload_conflicts(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    first = _capture(store)
    second = _capture(store)
    assert second.capture_id == first.capture_id
    assert len(store.effects_for_capture(first.capture_id)) == 1

    with pytest.raises(JournalCaptureConflict):
        store.create_capture(
            client_mutation_id="mutation-1",
            request_sha256="different",
            source_ref="wb-source://authority/item-2",
            representation_id="representation-2",
            submission_id="submission-other",
            command_id="command-other",
            source_effect_id="effect-other",
            day_id="2026-08-09",
            requested_target=CaptureTarget.LOG,
            mode=CaptureMode.DUMB,
            input_mode="paste",
            stated_at=None,
            submitted_at="2026-08-09T21:01:00+00:00",
            authorization_fingerprint="auth-fingerprint",
        )


def test_two_identical_text_occurrences_keep_distinct_identity(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    first = _capture(store, mutation="one")
    second = _capture(store, mutation="two")
    text_sha = hashlib.sha256(b"same text").hexdigest()

    first_entry = store.ensure_entry(
        capture_id=first.capture_id,
        entry_kind=CaptureTarget.RUNNING_NOTES,
        markdown="same text",
        content_sha256=text_sha,
        projection_marker="marker-one",
        created_at=first.submitted_at,
    )
    second_entry = store.ensure_entry(
        capture_id=second.capture_id,
        entry_kind=CaptureTarget.RUNNING_NOTES,
        markdown="same text",
        content_sha256=text_sha,
        projection_marker="marker-two",
        created_at=second.submitted_at,
    )

    assert first_entry.entry_id != second_entry.entry_id
    assert {item.entry_id for item in store.list_running_notes("2026-08-09")} == {
        first_entry.entry_id,
        second_entry.entry_id,
    }


def test_processing_and_projection_are_independent(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    capture = store.create_capture(
        client_mutation_id="smart",
        request_sha256="sha",
        source_ref="wb-source://authority/item",
        representation_id="representation",
        submission_id="submission",
        command_id="command",
        source_effect_id="source-effect",
        day_id="2026-08-09",
        requested_target=CaptureTarget.LOG,
        mode=CaptureMode.SMART,
        input_mode="paste",
        stated_at=None,
        submitted_at="2026-08-09T21:00:00+00:00",
        authorization_fingerprint="auth",
    )
    assert capture.processing_status is ProcessingState.PENDING
    assert {item.effect_type for item in store.effects_for_capture(capture.capture_id)} == {
        "materialize",
        "smart_annotate",
    }

    updated = store.set_processing(
        capture.capture_id,
        status=ProcessingState.FAILED,
        error_code="model_unavailable",
    )
    assert updated.persistence_status == "persisted"
    assert updated.processing_status is ProcessingState.FAILED


def test_document_source_dependency_transition_is_durable_and_idempotent(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    capture = _capture(store, mutation="source-transition")
    entry = store.ensure_entry(
        capture_id=capture.capture_id,
        entry_kind=CaptureTarget.RUNNING_NOTES,
        markdown="A managed note",
        content_sha256=hashlib.sha256(b"A managed note").hexdigest(),
        projection_marker="source-transition-marker",
        created_at=capture.submitted_at,
    )
    exact = store.record_document_binding(
        entry_id=entry.entry_id,
        binding_id="1" * 32,
        store_id="2" * 32,
        document_id="3" * 32,
        change_id="4" * 32,
        source_consumer_id="5" * 32,
        source_usage_id="6" * 32,
        cowork_href="/app/cowork?store_id=2&document_id=3",
        content_authority_epoch=1,
        entry_version=entry.version,
        inspection={"schema": "test/v1"},
    )
    assert exact.source_redaction_policy == "scrub"

    mixed, receipt = store.transition_document_source_usage(
        entry_id=entry.entry_id,
        binding_id=exact.binding_id,
        change_id="7" * 32,
        expected_prior_usage_id=exact.source_usage_id,
        next_usage_id="8" * 32,
        next_use_kind="mixed_derivative",
        next_disclosure_kind="semantic_derivative",
        next_redaction_policy="review",
    )
    replayed, same_receipt = store.transition_document_source_usage(
        entry_id=entry.entry_id,
        binding_id=exact.binding_id,
        change_id="7" * 32,
        expected_prior_usage_id=exact.source_usage_id,
        next_usage_id="8" * 32,
        next_use_kind="mixed_derivative",
        next_disclosure_kind="semantic_derivative",
        next_redaction_policy="review",
    )
    assert replayed == mixed
    assert same_receipt == receipt
    assert mixed.source_usage_id == "8" * 32
    assert mixed.source_use_kind == "mixed_derivative"
    assert mixed.source_disclosure_kind == "semantic_derivative"
    assert mixed.source_redaction_policy == "review"
    assert receipt.state == "mirror_updated"
    assert store.complete_document_source_usage_transition(
        receipt.transition_id
    ).state == "complete"
    attention = store.mark_document_source_review_required(
        entry.entry_id,
        details={
            "schema": "wb.source-maintenance-attention/v1",
            "kind": "source_redaction_review_required",
            "reason": "document_contains_direct_edits",
        },
    )
    assert attention.source_maintenance_state == "review_required"
    assert attention.source_maintenance["reason"] == "document_contains_direct_edits"
