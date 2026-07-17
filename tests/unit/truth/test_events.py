from __future__ import annotations

from types import SimpleNamespace

import pytest

from work_buddy.truth.events import TRUTH_EVENT_TYPES, emit_truth_event


TWELVE_DOC_EVENT_TYPES = (
    "truth.doc_registered",
    "truth.doc_imported",
    "truth.doc_materialized",
    "truth.doc_drift_detected",
    "truth.doc_reimported",
    "truth.doc_retired",
    "truth.doc_proposed",
    "truth.doc_proposal_decided",
    "truth.doc_proposal_applied",
    "truth.doc_proposal_expired",
    "truth.doc_expression_marked",
    "truth.doc_feedback_captured",
)


def test_truth_event_is_durable_scoped_and_subject_bound(monkeypatch) -> None:
    captured = []

    def fake_new_event(source, event_type, data, **kwargs):
        captured.append((source, event_type, data, kwargs))
        return SimpleNamespace(id="evt-1")

    monkeypatch.setattr("work_buddy.events.envelope.new_event", fake_new_event)
    monkeypatch.setattr("work_buddy.events.dispatcher.publish", lambda event: None)

    result = emit_truth_event(
        "truth.claim_confirmed",
        store_id="a" * 32,
        subject_kind="claim",
        subject_id="b" * 32,
        data={"status": "confirmed"},
    )

    assert result.published is True
    assert result.event_id == "evt-1"
    source, event_type, data, kwargs = captured[0]
    assert source == f"/wb/truth/{'a' * 32}"
    assert event_type == "truth.claim_confirmed"
    assert data == {"store_id": "a" * 32, "status": "confirmed"}
    assert kwargs["durable"] is True
    assert kwargs["subject"] == f"wb-truth://{'a' * 32}/claim/{'b' * 32}"


def test_truth_event_failure_is_non_authoritative(monkeypatch) -> None:
    monkeypatch.setattr(
        "work_buddy.events.dispatcher.publish",
        lambda event: (_ for _ in ()).throw(RuntimeError("spine unavailable")),
    )

    result = emit_truth_event(
        "truth.claim_proposed",
        store_id="a" * 32,
        subject_kind="claim",
        subject_id="b" * 32,
    )

    assert result.published is False
    assert result.event_id is None
    assert result.error == "spine unavailable"


def test_truth_event_data_cannot_spoof_store_identity(monkeypatch) -> None:
    captured = []

    def fake_new_event(source, event_type, data, **kwargs):
        captured.append(data)
        return SimpleNamespace(id="evt-2")

    monkeypatch.setattr("work_buddy.events.envelope.new_event", fake_new_event)
    monkeypatch.setattr("work_buddy.events.dispatcher.publish", lambda event: None)

    result = emit_truth_event(
        "truth.store_created",
        store_id="a" * 32,
        data={"store_id": "f" * 32, "profile": "test"},
    )

    assert result.published is True
    assert captured[0]["store_id"] == "a" * 32


def test_truth_event_rejects_unknown_type_and_partial_subject() -> None:
    with pytest.raises(ValueError, match="unsupported Truth event"):
        emit_truth_event("truth.unknown", store_id="a" * 32)
    with pytest.raises(ValueError, match="supplied together"):
        emit_truth_event(
            "truth.store_created",
            store_id="a" * 32,
            subject_kind="claim",
        )


def test_frozenset_carries_exactly_the_twelve_doc_event_names() -> None:
    # The frozenset is the SINGLE SOURCE OF TRUTH for truth.doc_* names.
    doc_names = {name for name in TRUTH_EVENT_TYPES if name.startswith("truth.doc_")}
    assert doc_names == set(TWELVE_DOC_EVENT_TYPES)
    assert len(TWELVE_DOC_EVENT_TYPES) == 12


@pytest.mark.parametrize("event_type", TWELVE_DOC_EVENT_TYPES)
def test_each_doc_event_publishes_with_a_doc_subject_uri(
    monkeypatch, event_type: str
) -> None:
    captured = []

    def fake_new_event(source, name, data, **kwargs):
        captured.append((name, kwargs))
        return SimpleNamespace(id="evt-doc")

    monkeypatch.setattr("work_buddy.events.envelope.new_event", fake_new_event)
    monkeypatch.setattr("work_buddy.events.dispatcher.publish", lambda event: None)

    result = emit_truth_event(
        event_type,
        store_id="a" * 32,
        subject_kind="document",
        subject_id="b" * 32,
    )

    assert result.published is True
    name, kwargs = captured[0]
    assert name == event_type
    assert kwargs["subject"] == f"wb-truth://{'a' * 32}/document/{'b' * 32}"
    assert kwargs["durable"] is True
