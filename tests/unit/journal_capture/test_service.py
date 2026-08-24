from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.models import CaptureMode, CaptureTarget, ProcessingState
from work_buddy.journal_capture.projection import view_snapshot
from work_buddy.journal_capture.service import (
    CommittedIngress,
    JournalCaptureService,
    SmartCaptureResult,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.settings import get_journal_day_window


def _day_id(local_date: str = "2026-08-09") -> str:
    window = get_journal_day_window(local_date)
    return f"journal-day:{local_date}:{window.timezone}:{window.boundary}"


@pytest.fixture(autouse=True)
def _fixed_projection_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "work_buddy.journal_capture.projection.current_day",
        lambda: {
            "dayId": _day_id(),
            "localDate": "2026-08-09",
            "timezone": "America/New_York",
            "dayBoundaryStart": "04:00",
            "windowStart": "2026-08-09T04:00:00-04:00",
            "windowEnd": "2026-08-10T04:00:00-04:00",
            "now": "2026-08-09T19:15:00+00:00",
        },
    )


def _vault(tmp_path: Path) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    path = journal / "2026-08-09.md"
    path.write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    return path


def _ingress(suffix: str = "one") -> CommittedIngress:
    return CommittedIngress(
        source_ref=f"wb-source://authority/item-{suffix}",
        representation_id=f"representation-{suffix}",
        submission_id=f"submission-{suffix}",
        command_id=f"command-{suffix}",
        effect_id=f"effect-{suffix}",
        authorization_fingerprint="fingerprint",
    )


def _write(_rel, abs_path, content, **_kw):
    abs_path.write_bytes(content.encode("utf-8"))
    return True


def test_direct_capture_persists_and_materializes_before_optional_processing(
    tmp_path, monkeypatch
):
    path = _vault(tmp_path)
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path))

    capture = service.accept(
        ingress=_ingress(),
        client_mutation_id="mutation-one",
        day_id=_day_id(),
        target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB,
        exact_text="  exact\ntext  ",
        input_mode="paste",
        stated_at="2026-08-09T15:15:00-04:00",
        submitted_at="2026-08-09T19:15:00+00:00",
    )

    assert capture.persistence_status == "persisted"
    assert capture.processing_status is ProcessingState.NOT_REQUESTED
    assert capture.entry_id is not None
    assert "  exact\ntext  " in path.read_text(encoding="utf-8")
    projected = view_snapshot(store)
    assert projected["runningNotes"]["items"][0]["markdown"] == "  exact\ntext  "
    reason = projected["runningNotes"]["access"]["reason"]
    assert reason == "Open a running note in Co-work to edit it."
    assert "authority" not in reason.lower()
    assert "migration" not in reason.lower()
    assert projected["capture"]["recentSubmissions"][0]["placementStatus"] == "placed"


def test_projection_failure_does_not_erase_persisted_capture(tmp_path):
    _vault(tmp_path).write_text("# unrelated\n", encoding="utf-8")
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path))

    capture = service.accept(
        ingress=_ingress("failure"),
        client_mutation_id="mutation-failure",
        day_id=_day_id(),
        target=CaptureTarget.LOG,
        mode=CaptureMode.DUMB,
        exact_text="saved first",
        input_mode="direct_entry",
        stated_at=None,
        submitted_at="2026-08-09T19:15:00+00:00",
    )

    assert capture.persistence_status == "persisted"
    projected = view_snapshot(store)
    recent = projected["capture"]["recentSubmissions"][0]
    assert recent["placementStatus"] == "failed"
    assert recent["errorMessage"].startswith("Saved")


def test_unavailable_smart_processing_preserves_direct_capture_and_is_not_advertised(
    tmp_path, monkeypatch
):
    _vault(tmp_path)
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path))

    capture = service.accept(
        ingress=_ingress("smart-unavailable"),
        client_mutation_id="mutation-smart-unavailable",
        day_id=_day_id(),
        target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.SMART,
        exact_text="preserve before optional processing",
        input_mode="direct_entry",
        stated_at=None,
        submitted_at="2026-08-09T19:15:00+00:00",
        run_smart=True,
    )

    assert capture.entry_id is not None
    assert capture.processing_status is ProcessingState.FAILED
    projected = view_snapshot(
        store,
        smart_processing_available=service.smart_processing_available,
    )
    targets = {item["targetId"]: item for item in projected["capture"]["targets"]}
    assert targets["auto"]["enabled"] is False
    assert targets["running_notes"]["supportedModes"] == ["dumb"]
    assert projected["capture"]["recentSubmissions"][0]["errorMessage"].startswith(
        "Saved"
    )


class _Smart:
    def process(self, *, capture, exact_text):
        assert exact_text == "Remember to fix the parser"
        return SmartCaptureResult(
            target=CaptureTarget.RUNNING_NOTES,
            summary="A follow-up to retain.",
            effects=("Routed to Running Notes",),
            producer_ref="agent-run:test",
            model_id="test-model",
            disclosure_manifest_sha256="d" * 64,
        )


def test_auto_route_is_one_retryable_effect_and_materializes_once(tmp_path, monkeypatch):
    _vault(tmp_path)
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalCaptureService(
        store,
        JournalContentAdapter(tmp_path),
        smart_processor=_Smart(),
    )
    capture = service.accept(
        ingress=_ingress("auto"),
        client_mutation_id="mutation-auto",
        day_id=_day_id(),
        target=CaptureTarget.AUTO,
        mode=CaptureMode.SMART,
        exact_text="Remember to fix the parser",
        input_mode="direct_entry",
        stated_at=None,
        submitted_at="2026-08-09T19:15:00+00:00",
        run_smart=True,
    )

    assert capture.processing_status is ProcessingState.SUCCEEDED
    assert capture.resolved_target is CaptureTarget.RUNNING_NOTES
    assert len(store.list_running_notes("2026-08-09")) == 1
    service.process_smart(capture.capture_id, exact_text="Remember to fix the parser")
    assert len(store.list_running_notes("2026-08-09")) == 1
