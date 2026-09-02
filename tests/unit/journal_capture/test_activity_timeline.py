from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from work_buddy import activity
from work_buddy.activity import EventSource, JournalEntry
from work_buddy.journal_capture.authority import JournalAuthorityCoordinator
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.mcp_server.context_wrappers import activity_timeline


def _record(
    store: JournalCaptureStore,
    *,
    local_date: str,
    value: str,
    mutation: str,
    authorship: str,
    item_kind: str = "record",
) -> None:
    JournalDomainService(store).create_native_item(
        local_date=local_date,
        item_kind=item_kind,
        plain_value=value,
        source_ref=f"wb-source://authority/{mutation}",
        interaction_behavior_id=(
            "provenance_only" if authorship == "ai" else "human_value"
        ),
        interaction_behavior_version=1,
        client_mutation_id=mutation,
        actor={"kind": "test"},
        authorship=authorship,
        review_state="unreviewed" if authorship == "ai" else "not_applicable",
    )


def _install_database_authority(
    monkeypatch: pytest.MonkeyPatch,
    store: JournalCaptureStore,
) -> None:
    monkeypatch.setattr(activity, "_journal_authority_mode", lambda: "database_only")
    monkeypatch.setattr(activity, "_native_journal_store", lambda: store)
    monkeypatch.setattr(
        JournalAuthorityCoordinator,
        "state",
        lambda _self: SimpleNamespace(mode="database_only"),
    )


def test_database_only_timeline_reads_native_days_and_never_markdown(
    tmp_path,
    monkeypatch,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    _install_database_authority(monkeypatch, store)
    _record(
        store,
        local_date="2026-08-26",
        value="4:45 PM - Earlier database-only work",
        mutation="activity-native-earlier",
        authorship="ai",
    )
    _record(
        store,
        local_date="2026-08-27",
        value="11:20 AM - Later human record",
        mutation="activity-native-later",
        authorship="human",
    )
    _record(
        store,
        local_date="2026-08-27",
        value="9:05 AM - Earlier agent record #wb/TODO",
        mutation="activity-native-agent",
        authorship="ai",
    )
    _record(
        store,
        local_date="2026-08-27",
        value="private generated artifact that is not a Log record",
        mutation="activity-native-generated",
        authorship="ai",
        item_kind="generated_artifact",
    )
    markdown_read = MagicMock(
        side_effect=AssertionError("retired Journal Markdown was read")
    )
    monkeypatch.setattr(JournalContentAdapter, "read_day", markdown_read)

    timeline = activity.infer_activity(
        since="2026-08-26T00:00:00",
        until="2026-08-27T23:59:59",
    )

    assert [entry.description for entry in timeline.journal_entries] == [
        "Earlier database-only work",
        "Earlier agent record",
        "Later human record",
    ]
    assert [entry.timestamp for entry in timeline.journal_entries] == [
        datetime(2026, 8, 26, 16, 45),
        datetime(2026, 8, 27, 9, 5),
        datetime(2026, 8, 27, 11, 20),
    ]
    assert [entry.source for entry in timeline.journal_entries] == [
        EventSource.JOURNAL_AGENT,
        EventSource.JOURNAL_AGENT,
        EventSource.JOURNAL_MANUAL,
    ]
    assert timeline.journal_entries[1].incomplete is True
    assert "private generated artifact" not in {
        event.summary for event in timeline.events
    }
    markdown_read.assert_not_called()

    rendered = activity_timeline(
        since="2026-08-26T00:00:00",
        until="2026-08-27T23:59:59",
    )
    assert isinstance(rendered, str)
    assert "Earlier database-only work" in rendered
    assert "[agent] [INCOMPLETE] Earlier agent record" in rendered
    assert "[manual] Later human record" in rendered


def test_database_only_timeline_projects_imported_log_section_from_sqlite(
    tmp_path,
    monkeypatch,
) -> None:
    store = JournalCaptureStore(tmp_path / "journal.db")
    _install_database_authority(monkeypatch, store)
    _record(
        store,
        local_date="2026-08-20",
        value=(
            "# **Log**\r\n"
            "* 9:00 AM - Imported agent entry. #wb/journal/log\r\n"
            "- 10:15 AM - Imported manual entry.\r\n"
            "# **Running Notes / Considerations**\r\n"
            "not part of the activity projection"
        ),
        mutation="activity-imported-log-section",
        authorship="unknown",
    )

    timeline = activity.infer_activity(
        since="2026-08-20T08:00:00",
        until="2026-08-20T11:00:00",
    )

    assert [(entry.description, entry.source) for entry in timeline.journal_entries] == [
        ("Imported agent entry", EventSource.JOURNAL_AGENT),
        ("Imported manual entry", EventSource.JOURNAL_MANUAL),
    ]


def test_legacy_projection_is_used_only_while_compatibility_authority_is_open(
    monkeypatch,
) -> None:
    legacy_entry = JournalEntry(
        timestamp=datetime(2026, 8, 27, 8, 0),
        description="compatibility entry",
        source=EventSource.JOURNAL_MANUAL,
    )
    legacy = MagicMock(return_value=[legacy_entry])
    native = MagicMock()
    monkeypatch.setattr(activity, "_legacy_journal_entries", legacy)
    monkeypatch.setattr(activity, "_native_journal_entries", native)
    monkeypatch.setattr(
        activity,
        "_journal_authority_mode",
        MagicMock(side_effect=["legacy_compatibility", "legacy_compatibility"]),
    )

    assert activity._journal_entries_for_dates(["2026-08-27"]) == [legacy_entry]
    legacy.assert_called_once_with(["2026-08-27"])
    native.assert_not_called()

    legacy.reset_mock()
    monkeypatch.setattr(activity, "_journal_authority_mode", lambda: "cutover_paused")
    assert activity._journal_entries_for_dates(["2026-08-27"]) == []
    legacy.assert_not_called()


def test_database_authority_failure_never_falls_back_to_markdown(monkeypatch) -> None:
    legacy = MagicMock(
        side_effect=AssertionError("database failure reopened retired Markdown")
    )
    monkeypatch.setattr(activity, "_journal_authority_mode", lambda: "database_only")
    monkeypatch.setattr(activity, "_legacy_journal_entries", legacy)
    monkeypatch.setattr(
        activity,
        "_native_journal_entries",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        activity._journal_entries_for_dates(["2026-08-27"])
    legacy.assert_not_called()
