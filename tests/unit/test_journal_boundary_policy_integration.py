from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import work_buddy.journal as journal
from work_buddy import config as wb_config
from work_buddy.settings import store
from work_buddy.settings import broker
from work_buddy.settings.registry import JOURNAL_DAY_BOUNDARY_ID


NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "settings.db")
    monkeypatch.setattr(wb_config, "_USER_TZ_CACHE", NY)


def test_today_and_yesterday_follow_logical_day_before_and_at_boundary(monkeypatch) -> None:
    monkeypatch.setattr(
        journal, "user_now", lambda: datetime(2026, 7, 15, 4, 59, tzinfo=NY)
    )
    assert journal.resolve_target_date("today").date == "2026-07-14"
    assert journal.resolve_target_date("today").ambiguous is False
    assert journal.resolve_target_date("yesterday").date == "2026-07-13"

    monkeypatch.setattr(
        journal, "user_now", lambda: datetime(2026, 7, 15, 5, 0, tzinfo=NY)
    )
    assert journal.resolve_target_date("today").date == "2026-07-15"


def test_past_journal_collection_window_is_offset_aware_and_dst_correct(
    monkeypatch, tmp_path
) -> None:
    journal_file = tmp_path / "journal" / "2026-03-07.md"
    journal_file.parent.mkdir(parents=True)
    journal_file.write_text("# **Log**\n\n# **Sign-In**\n", encoding="utf-8")
    monkeypatch.setattr(journal, "load_config", lambda: {"vault_root": str(tmp_path)})
    monkeypatch.setattr(
        journal, "user_now", lambda: datetime(2026, 3, 10, 12, 0, tzinfo=NY)
    )

    state = journal.read_journal_state("2026-03-07")
    assert state["day_boundary_start"] == "05:00"
    assert state["timezone"] == "America/New_York"
    assert state["collect_since"] == "2026-03-07T05:00:00-05:00"
    assert state["collect_until"] == "2026-03-08T05:00:00-04:00"
    start = datetime.fromisoformat(state["collect_since"])
    end = datetime.fromisoformat(state["collect_until"])
    assert (
        end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    ).total_seconds() == 23 * 60 * 60


def test_post_midnight_log_timestamp_uses_next_civil_date() -> None:
    content = "# **Log**\n* 1:30 AM - Still working. #wb/journal/log\n"
    result = journal.extract_last_log_timestamp(content, "2026-07-14")
    assert result is not None
    assert result.isoformat() == "2026-07-15T01:30:00-04:00"


def test_log_sorting_uses_bound_setting_instead_of_fixed_five_am(monkeypatch) -> None:
    monkeypatch.setattr(journal, "current_journal_boundary", lambda observed_at=None: "02:00")
    assert journal._effective_minutes("1:59 AM") == 24 * 60 + 119
    assert journal._effective_minutes("2:00 AM") == 120


def test_explicit_past_read_uses_persisted_pre_change_policy(monkeypatch, tmp_path) -> None:
    journal_file = tmp_path / "journal" / "2026-07-15.md"
    journal_file.parent.mkdir(parents=True)
    journal_file.write_text(
        "# **Log**\n* 2:00 AM - Continued after midnight.\n\n# **Sign-In**\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(journal, "load_config", lambda: {"vault_root": str(tmp_path)})
    monkeypatch.setattr(
        journal, "user_now", lambda: datetime(2026, 7, 18, 12, 0, tzinfo=NY)
    )
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=datetime(2026, 7, 15, 12, 0, tzinfo=NY),
    )
    broker.get_journal_day_binding(datetime(2026, 7, 16, 5, 0, tzinfo=NY))

    state = journal.read_journal_state("2026-07-15")
    assert state["day_boundary_start"] == "05:00"
    assert state["window_start"] == "2026-07-15T05:00:00-04:00"
    assert state["window_end"] == "2026-07-16T05:00:00-04:00"
    assert state["last_log_ts"] == "2026-07-16T02:00:00-04:00"
    assert state["collect_since"] == "2026-07-16T02:00:00-04:00"
    assert state["collect_until"] == "2026-07-16T05:00:00-04:00"


def test_append_to_old_day_sorts_with_that_days_persisted_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    import work_buddy.obsidian.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "is_available", lambda: False, raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: False, raising=False)
    journal_file = tmp_path / "journal" / "2026-07-15.md"
    journal_file.parent.mkdir(parents=True)
    journal_file.write_text(
        "# **Log**\n"
        "* 9:00 AM - Morning.\n"
        "* 11:00 PM - Late evening.\n"
        "\n# **Next Section**\n",
        encoding="utf-8",
    )
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=datetime(2026, 7, 15, 12, 0, tzinfo=NY),
    )
    broker.get_journal_day_binding(datetime(2026, 7, 16, 5, 0, tzinfo=NY))

    result = journal._append_to_journal_locked(
        [("4:30 AM", "Old-policy post-midnight entry")],
        journal_file,
        "2026-07-15",
    )
    assert result["success"] is True
    body = journal_file.read_text(encoding="utf-8").split("# **Log**", 1)[1]
    assert body.index("Late evening") < body.index("Old-policy post-midnight entry")
