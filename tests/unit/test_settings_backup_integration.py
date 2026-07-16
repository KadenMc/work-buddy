from __future__ import annotations

import json
import sqlite3
import tarfile


def test_settings_is_a_vital_resolvable_database() -> None:
    from work_buddy.backups.local import VITAL_DBS, _resolve_vital_dbs

    assert VITAL_DBS["settings"] == "db/settings"
    assert _resolve_vital_dbs()["settings"].name == "settings.db"


def test_restore_knows_and_applies_settings_migrations(tmp_path) -> None:
    from work_buddy.backups.local import VITAL_DBS
    from work_buddy.backups.restore import (
        _apply_migrations_inplace,
        _current_known_max_schema_versions,
    )
    from work_buddy.settings.migrations import SETTINGS_MIGRATIONS

    known = _current_known_max_schema_versions()
    assert set(known) == set(VITAL_DBS)
    assert known["settings"] == SETTINGS_MIGRATIONS.target_version

    db_path = tmp_path / "settings.db"
    sqlite3.connect(db_path).close()
    _apply_migrations_inplace("settings", db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == SETTINGS_MIGRATIONS.target_version
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'journal_day_policy_epoch'"
        ).fetchone()
    finally:
        conn.close()


def test_backup_restore_round_trip_preserves_settings_policy_state(
    monkeypatch, tmp_path
) -> None:
    from work_buddy.backups import local
    from work_buddy.backups.restore import _apply_migrations_inplace
    from work_buddy.settings.migrations import SETTINGS_MIGRATIONS

    live_db = tmp_path / "live" / "settings.db"
    live_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(live_db)
    try:
        SETTINGS_MIGRATIONS.run(conn)
        conn.execute(
            """
            INSERT INTO setting_value_state (
                setting_id, scope, scope_id, bootstrap_default_value_json,
                bootstrap_source, value_version, active_timezone,
                active_value_json, pending_value_json, pending_source,
                pending_timezone, effective_at, revision, updated_at
            ) VALUES (
                'wb.journal.day-boundary', 'profile', 'default', '"05:00"',
                'config-bootstrap', 1, 'America/New_York', '"04:00"',
                '"03:00"', 'profile', 'America/New_York',
                '2026-07-17T04:00:00-04:00', 2,
                '2026-07-16T12:00:00-04:00'
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO journal_day_policy_epoch (
                effective_local_date, window_start, boundary, timezone,
                setting_revision, created_at
            ) VALUES (?, ?, ?, 'America/New_York', ?, ?)
            """,
            [
                (None, None, "05:00", 0, "2026-07-15T12:00:00-04:00"),
                (
                    "2026-07-16",
                    "2026-07-16T05:00:00-04:00",
                    "04:00",
                    1,
                    "2026-07-16T05:00:00-04:00",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(local, "_resolve_vital_dbs", lambda: {"settings": live_db})
    monkeypatch.setattr(local, "data_dir", lambda name="": tmp_path / name)
    result = local.run_backup(manual=True)
    manifest = json.loads(result["manifest"])
    assert manifest["schema_versions"]["settings"] == SETTINGS_MIGRATIONS.target_version
    assert manifest["row_counts"]["settings"]["setting_value_state"] == 1
    assert manifest["row_counts"]["settings"]["journal_day_policy_epoch"] == 2

    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        assert {"MANIFEST.json", "settings.db"}.issubset(archive.getnames())
        archive.extract("settings.db", path=restore_dir)
    restored_db = restore_dir / "settings.db"
    _apply_migrations_inplace("settings", restored_db)

    conn = sqlite3.connect(restored_db)
    try:
        state = conn.execute(
            """
            SELECT active_value_json, pending_value_json, pending_timezone,
                   effective_at, revision
            FROM setting_value_state
            """
        ).fetchone()
        epochs = conn.execute(
            """
            SELECT effective_local_date, window_start, boundary, timezone,
                   setting_revision
            FROM journal_day_policy_epoch ORDER BY sequence
            """
        ).fetchall()
    finally:
        conn.close()
    assert state == (
        '"04:00"',
        '"03:00"',
        "America/New_York",
        "2026-07-17T04:00:00-04:00",
        2,
    )
    assert epochs == [
        (None, None, "05:00", "America/New_York", 0),
        (
            "2026-07-16",
            "2026-07-16T05:00:00-04:00",
            "04:00",
            "America/New_York",
            1,
        ),
    ]
