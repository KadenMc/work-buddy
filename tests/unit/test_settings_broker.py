from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from work_buddy import config as wb_config
from work_buddy.settings import broker, store
from work_buddy.settings.registry import JOURNAL_DAY_BOUNDARY_ID


NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    store._schema_ready.clear()
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "settings.db")
    monkeypatch.setattr(wb_config, "_USER_TZ_CACHE", NY)
    yield
    store._schema_ready.clear()


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=NY)


def _value(at: datetime):
    return broker.get_values(observed_at=at)[0]["values"][0]


def test_registry_defines_one_app_owned_profile_setting_with_one_canonical_placement() -> None:
    payload = broker.get_registry()
    assert payload["registry_revision"] == "settings-registry:1"
    assert [item["setting_id"] for item in payload["definitions"]] == [
        JOURNAL_DAY_BOUNDARY_ID
    ]
    definition = payload["definitions"][0]
    assert definition["owner"] == {
        "kind": "app",
        "id": "wb.journal",
        "label": "Journal",
    }
    assert definition["provenance"]["label"] == "Journal"
    assert definition["value_version"] == 1
    assert definition["default_value"] == "05:00"
    assert definition["allowed_scopes"] == ["profile"]
    assert [item["context_id"] for item in payload["placements"]] == [
        "wb.settings.app.journal"
    ]


def test_default_snapshot_carries_timezone_revision_and_impact() -> None:
    payload, events = broker.get_values(
        context_id="wb.settings.app.journal", observed_at=_at(15, 12)
    )
    assert events == []
    assert payload["timezone"] == "America/New_York"
    value = payload["values"][0]
    assert value["effective_value"] == "05:00"
    assert value["configured_value"] == "05:00"
    assert value["source"] == "default"
    assert value["value_version"] == 1
    assert value["default_value"] == "05:00"
    assert value["default_source"] == "config-bootstrap"
    assert value["revision"] == "value:0"
    assert value["pending_value"] is None
    assert value["impact_preview"]["current_day"]["window_start"].endswith("-04:00")


@pytest.mark.parametrize("invalid", [None, 500, "5:00", "24:00", "05:60"])
def test_invalid_boundary_writes_are_rejected(invalid) -> None:
    with pytest.raises(broker.SettingsError) as raised:
        broker.update_value(
            JOURNAL_DAY_BOUNDARY_ID,
            scope="profile",
            value=invalid,
            expected_revision="value:0",
            observed_at=_at(15, 12),
        )
    assert raised.value.code == "validation_error"
    assert raised.value.field == "value"


def test_wrong_scope_and_read_only_writes_are_rejected() -> None:
    with pytest.raises(broker.SettingsError) as raised:
        broker.update_value(
            JOURNAL_DAY_BOUNDARY_ID,
            scope="view",
            value="04:00",
            expected_revision="value:0",
            observed_at=_at(15, 12),
        )
    assert raised.value.code == "validation_error"

    with pytest.raises(broker.SettingsError) as raised:
        broker.update_value(
            JOURNAL_DAY_BOUNDARY_ID,
            scope="profile",
            value="04:00",
            expected_revision="value:0",
            observed_at=_at(15, 12),
            read_only=True,
        )
    assert raised.value.code == "read_only"


def test_write_is_pending_until_next_safe_boundary_then_promotes() -> None:
    pending, event = broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="07:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    assert pending["effective_value"] == "05:00"
    assert pending["configured_value"] == "07:00"
    assert pending["pending_value"] == "07:00"
    assert pending["effective_at"] == "2026-07-16T07:00:00-04:00"
    assert pending["revision"] == "value:1"
    assert event["apply_status"] == "pending"
    assert pending["impact_preview"]["pending_day"] == {
        "local_date": "2026-07-16",
        "timezone": "America/New_York",
        "day_boundary_start": "07:00",
        "window_start": "2026-07-16T07:00:00-04:00",
        "window_end": "2026-07-17T07:00:00-04:00",
    }

    before = _value(_at(16, 6, 59))
    assert before["effective_value"] == "05:00"
    assert before["pending_value"] == "07:00"

    payload, events = broker.get_values(observed_at=_at(16, 7))
    effective = payload["values"][0]
    assert effective["effective_value"] == "07:00"
    assert effective["pending_value"] is None
    assert effective["revision"] == "value:2"
    assert events[0]["reason"] == "pending-value-applied"


def test_optimistic_conflict_returns_authoritative_value() -> None:
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    with pytest.raises(broker.SettingsError) as raised:
        broker.update_value(
            JOURNAL_DAY_BOUNDARY_ID,
            scope="profile",
            value="03:00",
            expected_revision="value:0",
            observed_at=_at(15, 12, 1),
        )
    assert raised.value.code == "revision_conflict"
    assert raised.value.value["revision"] == "value:1"
    assert raised.value.value["pending_value"] == "04:00"


def test_reset_cancels_pending_or_schedules_default_without_rewriting_history() -> None:
    pending, _ = broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    cancelled, event = broker.reset_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        expected_revision=pending["revision"],
        observed_at=_at(15, 12, 1),
    )
    assert cancelled["effective_value"] == "05:00"
    assert cancelled["pending_value"] is None
    assert cancelled["revision"] == "value:2"
    assert event["reason"] == "pending-value-cancelled"

    scheduled, _ = broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision=cancelled["revision"],
        observed_at=_at(15, 12, 2),
    )
    active = _value(_at(16, 5))
    assert active["effective_value"] == "04:00"
    reset, _ = broker.reset_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        expected_revision=active["revision"],
        observed_at=_at(16, 6),
    )
    assert reset["effective_value"] == "04:00"
    assert reset["pending_value"] == "05:00"
    assert reset["configured_source"] == "default"


def test_backend_journal_binding_returns_real_instants_not_opened_at() -> None:
    binding, _ = broker.get_journal_day_binding(_at(15, 2))
    assert binding == {
        "local_date": "2026-07-14",
        "timezone": "America/New_York",
        "day_boundary_start": "05:00",
        "window_start": "2026-07-14T05:00:00-04:00",
        "window_end": "2026-07-15T05:00:00-04:00",
        "boundary_setting_revision": "value:0",
        "pending_day_boundary_start": None,
        "boundary_effective_at": None,
        "configured_timezone": "America/New_York",
        "diagnostics": [],
    }
    assert "opened_at" not in binding


def test_later_boundary_extends_old_day_instead_of_opening_then_reinterpreting_it() -> None:
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="07:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )

    bridge, _ = broker.get_journal_day_binding(_at(16, 6))
    assert bridge["local_date"] == "2026-07-15"
    assert bridge["day_boundary_start"] == "05:00"
    assert bridge["window_start"] == "2026-07-15T05:00:00-04:00"
    assert bridge["window_end"] == "2026-07-16T07:00:00-04:00"

    first_new_day, _ = broker.get_journal_day_binding(_at(16, 7))
    assert first_new_day["local_date"] == "2026-07-16"
    assert first_new_day["day_boundary_start"] == "07:00"
    assert first_new_day["window_start"] == "2026-07-16T07:00:00-04:00"
    assert first_new_day["window_end"] == "2026-07-17T07:00:00-04:00"


def test_pending_preview_current_day_matches_later_boundary_bridge() -> None:
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="07:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    current = _value(_at(16, 6))["impact_preview"]["current_day"]
    assert current == {
        "local_date": "2026-07-15",
        "timezone": "America/New_York",
        "day_boundary_start": "05:00",
        "window_start": "2026-07-15T05:00:00-04:00",
        "window_end": "2026-07-16T07:00:00-04:00",
    }


def test_earlier_boundary_creates_non_retroactive_first_transition_day() -> None:
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )

    before, _ = broker.get_journal_day_binding(_at(16, 4, 30))
    assert before["local_date"] == "2026-07-15"
    assert before["window_end"] == "2026-07-16T05:00:00-04:00"

    first_new_day, _ = broker.get_journal_day_binding(_at(16, 5))
    assert first_new_day["local_date"] == "2026-07-16"
    assert first_new_day["day_boundary_start"] == "04:00"
    # The setting is active, but 04:00-05:00 was never reassigned away from
    # the day that just closed.
    assert first_new_day["window_start"] == "2026-07-16T05:00:00-04:00"
    assert first_new_day["window_end"] == "2026-07-17T04:00:00-04:00"

    next_regular_day, _ = broker.get_journal_day_binding(_at(17, 4))
    assert next_regular_day["local_date"] == "2026-07-17"
    assert next_regular_day["window_start"] == "2026-07-17T04:00:00-04:00"


def test_earlier_boundary_preview_starts_at_actual_safe_transition() -> None:
    pending, _ = broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    assert pending["effective_at"] == "2026-07-16T05:00:00-04:00"
    assert pending["impact_preview"]["pending_day"] == {
        "local_date": "2026-07-16",
        "timezone": "America/New_York",
        "day_boundary_start": "04:00",
        "window_start": "2026-07-16T05:00:00-04:00",
        "window_end": "2026-07-17T04:00:00-04:00",
    }


def test_policy_history_keeps_past_and_bridge_days_stable_across_reopen() -> None:
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    broker.get_journal_day_binding(_at(16, 5))  # lazily promote and persist epoch

    old_day = broker.get_journal_day_window("2026-07-15", observed_at=_at(18, 12))
    bridge_day = broker.get_journal_day_window("2026-07-16", observed_at=_at(18, 12))
    assert old_day.boundary == "05:00"
    assert old_day.start.isoformat() == "2026-07-15T05:00:00-04:00"
    assert old_day.end.isoformat() == "2026-07-16T05:00:00-04:00"
    assert bridge_day.boundary == "04:00"
    assert bridge_day.start.isoformat() == "2026-07-16T05:00:00-04:00"
    assert bridge_day.end.isoformat() == "2026-07-17T04:00:00-04:00"
    assert old_day.end.astimezone(ZoneInfo("UTC")) == bridge_day.start.astimezone(
        ZoneInfo("UTC")
    )

    # Every call opens a fresh SQLite connection; the second lookup verifies
    # reconstruction from disk rather than an in-memory transition object.
    reopened = broker.get_journal_day_window("2026-07-16", observed_at=_at(19, 12))
    assert reopened.as_dict() == bridge_day.as_dict()
    conn = store.get_connection()
    try:
        epochs = conn.execute(
            "SELECT effective_local_date, boundary, timezone "
            "FROM journal_day_policy_epoch ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in epochs] == [
        (None, "05:00", "America/New_York"),
        ("2026-07-16", "04:00", "America/New_York"),
    ]


def test_config_boundary_is_one_time_bootstrap_not_a_live_competing_source(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        wb_config,
        "load_config",
        lambda: {"journal": {"day_boundary": "06:00"}},
    )
    first = _value(_at(15, 12))
    assert first["effective_value"] == "06:00"
    assert first["default_value"] == "06:00"
    assert first["default_source"] == "config-bootstrap"
    assert first["revision"] == "value:0"

    # Once bootstrapped, Settings owns the canonical value. A later direct
    # file edit neither changes it nor creates an unversioned competing write.
    monkeypatch.setattr(
        wb_config,
        "load_config",
        lambda: {"journal": {"day_boundary": "03:00"}},
    )
    reopened = _value(_at(15, 12, 1))
    assert reopened["effective_value"] == "06:00"
    assert reopened["default_value"] == "06:00"
    assert reopened["revision"] == "value:0"


def test_scheduled_transition_persists_timezone_and_reports_later_config_drift(
    monkeypatch,
) -> None:
    pending, _ = broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="07:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    assert pending["pending_timezone"] == "America/New_York"
    assert pending["effective_at"] == "2026-07-16T07:00:00-04:00"

    los_angeles = ZoneInfo("America/Los_Angeles")
    monkeypatch.setattr(wb_config, "_USER_TZ_CACHE", los_angeles)
    before, _ = broker.get_values(
        observed_at=datetime(2026, 7, 16, 3, 0, tzinfo=los_angeles)
    )
    assert before["timezone"] == "America/New_York"
    assert before["configured_timezone"] == "America/Los_Angeles"
    assert before["values"][0]["effective_at"] == "2026-07-16T07:00:00-04:00"
    assert before["diagnostics"][0]["code"] == "timezone_config_drift"

    binding, _ = broker.get_journal_day_binding(
        datetime(2026, 7, 16, 4, 0, tzinfo=los_angeles)
    )
    assert binding["timezone"] == "America/New_York"
    assert binding["window_start"] == "2026-07-16T07:00:00-04:00"
    assert binding["diagnostics"][0] == {
        "code": "timezone_config_drift",
        "active_timezone": "America/New_York",
        "configured_timezone": "America/Los_Angeles",
        "message": (
            "The configured Work Buddy timezone differs from the Journal policy "
            "timezone. Existing and current Journal days remain on the persisted "
            "policy until a formal timezone migration is applied."
        ),
    }


@pytest.mark.parametrize("operation", ["patch", "reset"])
def test_stale_mutation_that_promotes_due_value_publishes_once(
    monkeypatch,
    operation: str,
) -> None:
    broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    published: list[dict] = []
    monkeypatch.setattr(broker, "publish_change", lambda event: published.append(event) if event else None)

    with pytest.raises(broker.SettingsError) as raised:
        if operation == "patch":
            broker.update_value(
                JOURNAL_DAY_BOUNDARY_ID,
                scope="profile",
                value="03:00",
                expected_revision="value:1",
                observed_at=_at(16, 5),
            )
        else:
            broker.reset_value(
                JOURNAL_DAY_BOUNDARY_ID,
                scope="profile",
                expected_revision="value:1",
                observed_at=_at(16, 5),
            )

    assert raised.value.code == "revision_conflict"
    assert [event["reason"] for event in published] == ["pending-value-applied"]


def test_early_settings_database_migrates_value_identity_and_bootstrap(tmp_path) -> None:
    db_path = tmp_path / "settings.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE setting_value_state (
                setting_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                active_value_json TEXT,
                pending_value_json TEXT,
                pending_source TEXT,
                effective_at TEXT,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (setting_id, scope, scope_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO setting_value_state (
                setting_id, scope, scope_id, revision, updated_at
            ) VALUES (?, 'profile', 'default', 0, ?)
            """,
            (JOURNAL_DAY_BOUNDARY_ID, _at(15, 12).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    migrated = _value(_at(15, 12, 1))
    assert migrated["value_version"] == 1
    assert migrated["default_value"] == "05:00"
    assert migrated["default_source"] == "config-migration"
    assert migrated["revision"] == "value:0"


def test_preview_is_side_effect_free_and_uses_authoritative_bridge_math(
    monkeypatch,
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(
        broker,
        "publish_change",
        lambda event: published.append(event) if event else None,
    )
    preview = broker.preview_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    assert preview["value_revision"] == "value:0"
    assert preview["timezone"] == "America/New_York"
    assert preview["preview"]["effective_at"] == "2026-07-16T05:00:00-04:00"
    assert preview["preview"]["impact_preview"]["pending_day"][
        "window_start"
    ] == "2026-07-16T05:00:00-04:00"
    assert preview["preview"]["impact_preview"]["pending_day"][
        "window_end"
    ] == "2026-07-17T04:00:00-04:00"
    assert published == []

    conn = store.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM setting_value_state").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_day_policy_epoch"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_preview_is_dst_aware_without_persisting_a_revision() -> None:
    observed = datetime(2026, 3, 7, 12, 0, tzinfo=NY)
    preview = broker.preview_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=observed,
    )
    current = preview["preview"]["impact_preview"]["current_day"]
    pending = preview["preview"]["impact_preview"]["pending_day"]
    assert current["window_start"] == "2026-03-07T05:00:00-05:00"
    assert current["window_end"] == "2026-03-08T05:00:00-04:00"
    assert pending["window_start"] == "2026-03-08T05:00:00-04:00"
    assert pending["window_end"] == "2026-03-09T04:00:00-04:00"
    assert _value(observed)["revision"] == "value:0"


def test_preview_rejects_stale_revision_and_read_only() -> None:
    with pytest.raises(broker.SettingsError) as stale:
        broker.preview_value(
            JOURNAL_DAY_BOUNDARY_ID,
            scope="profile",
            value="04:00",
            expected_revision="value:99",
            observed_at=_at(15, 12),
        )
    assert stale.value.code == "revision_conflict"
    assert stale.value.value["revision"] == "value:0"

    with pytest.raises(broker.SettingsError) as read_only:
        broker.preview_value(
            JOURNAL_DAY_BOUNDARY_ID,
            scope="profile",
            value="04:00",
            expected_revision="value:0",
            observed_at=_at(15, 12),
            read_only=True,
        )
    assert read_only.value.code == "read_only"


def test_preview_first_after_boundary_commits_due_promotion_once(monkeypatch) -> None:
    pending, _ = broker.update_value(
        JOURNAL_DAY_BOUNDARY_ID,
        scope="profile",
        value="04:00",
        expected_revision="value:0",
        observed_at=_at(15, 12),
    )
    published: list[dict] = []
    monkeypatch.setattr(
        broker,
        "publish_change",
        lambda event: published.append(event) if event else None,
    )
    with pytest.raises(broker.SettingsError) as conflict:
        broker.preview_value(
            JOURNAL_DAY_BOUNDARY_ID,
            scope="profile",
            value="03:00",
            expected_revision=pending["revision"],
            observed_at=_at(16, 5),
        )
    assert conflict.value.code == "revision_conflict"
    assert conflict.value.value["revision"] == "value:2"
    assert [event["reason"] for event in published] == ["pending-value-applied"]
    authoritative = _value(_at(16, 5, 1))
    assert authoritative["effective_value"] == "04:00"
    assert authoritative["revision"] == "value:2"
    assert len(published) == 1
