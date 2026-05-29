"""End-to-end smoke tests for the unified artifact lifecycle system.

These exercise the full composition: a Storage backend + a Lifecycle
(trigger + action + optional retention predicate) + optional
Provenance, wrapped in an Artifact, with .prune() actually walking
records, identifying expired ones, and acting on them.

Coverage:
    1. Construction-time coherence validation (IncoherentComposition)
    2. StorageTrait gating (UnsupportedOperation)
    3. UTC datetime handling (boundary-inclusive, naive-as-UTC)
    4. End-to-end prune for each backend × representative lifecycle pair
    5. Conditional retention (predicate skips otherwise-expired records)
    6. Registry sweep_all + per-name scoped sweep
    7. artifact_registry_dump shape

This test file is the foundation's smoke test — proves that everything
composes correctly before any consumer migrations land in Phase D.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.artifacts import (
    Artifact,
    StorageTrait,
    Delete,
    DirectoryTreeStorage,
    DirShape,
    FilesystemStorage,
    IncoherentComposition,
    JsonRecordsShape,
    JsonRecordsStorage,
    JsonlStorage,
    Lifecycle,
    MtimeWindow,
    Operation,
    PerRecordTtl,
    PerTypeTtl,
    SessionTagged,
    SqliteRollupStorage,
    SqliteRowsStorage,
    TimeWindow,
    TransformAndDelete,
    UnsupportedOperation,
    artifact_registry_dump,
    expires_at_iso,
    format_for_user,
    is_expired,
    register_artifact,
    sweep_all,
)
from work_buddy.artifacts.registry import _reset_for_tests


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty global registry.

    We also mark consumer modules as already-loaded so ``sweep_all``
    doesn't auto-import them mid-test (which would re-populate the
    registry with the production artifacts and break tests that expect
    only their own registrations to be visible).
    """
    import work_buddy.artifacts.registry as _reg

    _reset_for_tests()
    _reg._consumers_loaded = True  # opt out of auto-load during tests
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# UTC expiry helpers
# ---------------------------------------------------------------------------


def test_is_expired_boundary_inclusive() -> None:
    """The exact moment of expiry counts as expired (the t-96e45c67 fix)."""
    expires = "2026-05-08T12:00:00+00:00"
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert is_expired(expires, now=now) is True


def test_is_expired_strictly_past() -> None:
    expires = "2026-05-08T12:00:00+00:00"
    later = datetime(2026, 5, 8, 12, 0, 1, tzinfo=timezone.utc)
    assert is_expired(expires, now=later) is True


def test_is_expired_not_yet() -> None:
    expires = "2026-05-08T12:00:00+00:00"
    earlier = datetime(2026, 5, 8, 11, 59, 59, tzinfo=timezone.utc)
    assert is_expired(expires, now=earlier) is False


def test_is_expired_naive_iso_treated_as_utc() -> None:
    """Legacy naive ISO is treated as UTC for backwards compat."""
    expires = "2026-05-08T12:00:00"  # no offset
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert is_expired(expires, now=now) is True


def test_is_expired_empty_string_returns_false() -> None:
    assert is_expired("", now=datetime.now(timezone.utc)) is False


def test_expires_at_iso_is_utc_aware() -> None:
    """Computed expires_at carries an explicit UTC offset."""
    iso = expires_at_iso(ttl_days=1)
    assert "+00:00" in iso or iso.endswith("Z")


def test_format_for_user_returns_str() -> None:
    s = format_for_user("2026-05-08T12:00:00+00:00", tz="UTC")
    assert "2026-05-08" in s
    assert "12:00:00" in s


# ---------------------------------------------------------------------------
# Construction-time coherence
# ---------------------------------------------------------------------------


def test_per_record_ttl_with_filesystem_storage_raises(tmp_path: Path) -> None:
    """PerRecordTtl needs records-shaped storage; filesystem is blob-shaped."""
    fs = FilesystemStorage(data_root=tmp_path)
    bad_lifecycle = Lifecycle(
        trigger=PerRecordTtl(ttl_field="expires_at"),
        action=Delete(),
    )
    with pytest.raises(IncoherentComposition):
        Artifact(name="bad", storage=fs, lifecycle=bad_lifecycle)


def test_transform_and_delete_with_filesystem_raises(tmp_path: Path) -> None:
    """TransformAndDelete needs records storage."""
    fs = FilesystemStorage(data_root=tmp_path)
    lifecycle = Lifecycle(
        trigger=TimeWindow(timestamp_field="created_at", window_days=7),
        action=TransformAndDelete(transform_fn=lambda conn, dry_run: {}),
    )
    with pytest.raises(IncoherentComposition):
        Artifact(name="bad", storage=fs, lifecycle=lifecycle)


def test_filesystem_with_per_type_ttl_is_coherent(tmp_path: Path) -> None:
    """The intended pairing for filesystem artifacts."""
    fs = FilesystemStorage(data_root=tmp_path)
    lifecycle = Lifecycle(
        trigger=PerTypeTtl(ttl_days_by_type={"scratch": 3}),
        action=Delete(),
    )
    a = Artifact(name="filesystem", storage=fs, lifecycle=lifecycle)
    assert StorageTrait.PER_TYPE_TTL in a.capabilities


# ---------------------------------------------------------------------------
# StorageTrait gating
# ---------------------------------------------------------------------------


def test_delete_where_on_filesystem_raises(tmp_path: Path) -> None:
    """Filesystem doesn't declare BULK_PRUNEABLE."""
    fs = FilesystemStorage(data_root=tmp_path)
    lifecycle = Lifecycle(
        trigger=PerTypeTtl(ttl_days_by_type={"scratch": 3}),
        action=Delete(),
    )
    a = Artifact(name="filesystem", storage=fs, lifecycle=lifecycle)
    with pytest.raises(UnsupportedOperation) as exc_info:
        a.delete_where(lambda r: True)
    assert exc_info.value.missing == StorageTrait.BULK_PRUNEABLE


def test_list_by_session_without_provenance_raises(tmp_path: Path) -> None:
    """SESSION_TAGGED capability is required for list_by_session."""
    fs = FilesystemStorage(data_root=tmp_path)
    lifecycle = Lifecycle(
        trigger=PerTypeTtl(ttl_days_by_type={"scratch": 3}),
        action=Delete(),
    )
    a = Artifact(name="filesystem", storage=fs, lifecycle=lifecycle)
    with pytest.raises(UnsupportedOperation):
        a.list_by_session("any-id")


# ---------------------------------------------------------------------------
# End-to-end prune per backend
# ---------------------------------------------------------------------------


def test_prune_json_records_dict_per_record_ttl(tmp_path: Path) -> None:
    """Cache-shaped: dict of records, each with expires_at."""
    cache_path = tmp_path / "cache.json"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    cache_path.write_text(json.dumps({
        "expired-key": {"value": "stale", "expires_at": yesterday},
        "fresh-key": {"value": "live", "expires_at": tomorrow},
    }))

    storage = JsonRecordsStorage(path=cache_path, shape=JsonRecordsShape.DICT)
    lifecycle = Lifecycle(
        trigger=PerRecordTtl(ttl_field="expires_at"),
        action=Delete(),
    )
    artifact = Artifact(name="test-cache", storage=storage, lifecycle=lifecycle)

    result = artifact.prune(dry_run=False)
    assert result.pruned == 1
    after = json.loads(cache_path.read_text())
    assert "fresh-key" in after
    assert "expired-key" not in after


def test_prune_jsonl_time_window(tmp_path: Path) -> None:
    """Log-shaped: JSONL with timestamp field, window-based cutoff."""
    log_path = tmp_path / "events.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    log_path.write_text(
        json.dumps({"id": "old1", "timestamp": old}) + "\n"
        + json.dumps({"id": "recent1", "timestamp": recent}) + "\n"
    )

    storage = JsonlStorage(path=log_path)
    lifecycle = Lifecycle(
        trigger=TimeWindow(timestamp_field="timestamp", window_days=30),
        action=Delete(),
    )
    artifact = Artifact(name="test-log", storage=storage, lifecycle=lifecycle)

    result = artifact.prune(dry_run=False)
    assert result.pruned == 1
    text = log_path.read_text()
    assert "recent1" in text
    assert "old1" not in text


def test_prune_sqlite_rows_per_record_ttl_with_retention(tmp_path: Path) -> None:
    """Messaging-shaped: rows with TTL on created_at + status retention."""
    db = tmp_path / "msgs.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, status TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?)",
            [
                ("a", "pending", (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()),
                ("b", "done", (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()),
                ("c", "done", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()),
            ],
        )
        conn.commit()

    storage = SqliteRowsStorage(
        db_path=db, table="messages", id_column="id", vacuum_on_delete=False
    )
    lifecycle = Lifecycle(
        trigger=PerRecordTtl(ttl_field="created_at", default_ttl_days=30),
        action=Delete(),
        retention_predicate=lambda r: r.get("status") == "pending",
    )
    artifact = Artifact(name="messages", storage=storage, lifecycle=lifecycle)

    assert StorageTrait.CONDITIONAL_RETENTION in artifact.capabilities
    result = artifact.prune(dry_run=False)
    # Only 'b' should be deleted: 'a' is pending (retained), 'c' is fresh.
    assert result.pruned == 1
    with sqlite3.connect(str(db)) as conn:
        remaining = sorted(r[0] for r in conn.execute("SELECT id FROM messages"))
    assert remaining == ["a", "c"]


def test_prune_filesystem_per_type_ttl(tmp_path: Path) -> None:
    """Filesystem artifacts: write some, age them, prune."""
    storage = FilesystemStorage(data_root=tmp_path)
    # Write a fresh artifact (default scratch=3d TTL)
    fresh = storage.save("fresh content", "scratch", "fresh-test", "txt")
    # Write an artifact then expire it manually by editing meta
    stale = storage.save("stale content", "scratch", "stale-test", "txt")
    meta_raw = json.loads(stale.meta_path.read_text())
    meta_raw["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat()
    stale.meta_path.write_text(json.dumps(meta_raw))

    lifecycle = Lifecycle(
        trigger=PerTypeTtl(ttl_days_by_type={"scratch": 3}),
        action=Delete(),
    )
    artifact = Artifact(name="filesystem", storage=storage, lifecycle=lifecycle)

    result = artifact.prune(dry_run=False)
    assert result.pruned == 1
    # Fresh survives, stale gone
    assert fresh.path.exists()
    assert not stale.path.exists()


def test_prune_directory_tree_session_dirs_with_activity_check(tmp_path: Path) -> None:
    """Sessions shape: stale by manifest-age but kept if files modified recently.

    Setup:
        * old_session: manifest 30d old + ALL file mtimes 30d old → pruned
        * active_session: manifest 30d old but files just modified →
          kept (activity_check overrides the trigger).

    The trigger fires on ``created_at`` (manifest field) and the
    activity_check defers based on ``_latest_mtime`` (DirectoryTree's
    computed freshest file mtime in the dir tree).
    """
    import os

    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    thirty_days_ago_ts = thirty_days_ago.timestamp()
    old_created_iso = thirty_days_ago.isoformat()

    # Old session — both manifest age and file mtimes are stale.
    old_session = tmp_path / "20250101-100000_old1"
    old_session.mkdir()
    old_manifest = old_session / "manifest.json"
    old_manifest.write_text(
        json.dumps({"session_id": "old1", "created_at": old_created_iso})
    )
    os.utime(old_manifest, (thirty_days_ago_ts, thirty_days_ago_ts))

    # Active session — manifest old, but a file was modified recently.
    active_session = tmp_path / "20250101-100000_active1"
    active_session.mkdir()
    active_manifest = active_session / "manifest.json"
    active_manifest.write_text(
        json.dumps({"session_id": "active1", "created_at": old_created_iso})
    )
    os.utime(active_manifest, (thirty_days_ago_ts, thirty_days_ago_ts))
    # data.txt with current mtime — proves recent activity
    (active_session / "data.txt").write_text("recent")

    storage = DirectoryTreeStorage(root=tmp_path, shape=DirShape.SESSION_DIRS)

    def has_recent_activity(record: dict, cutoff: datetime) -> bool:
        from work_buddy.artifacts.expiry import _parse_to_utc

        latest = _parse_to_utc(record.get("_latest_mtime", ""))
        if latest is None:
            return False
        return latest >= cutoff

    lifecycle = Lifecycle(
        trigger=MtimeWindow(
            mtime_field="created_at",
            max_age_days=14,
            activity_check=has_recent_activity,
        ),
        action=Delete(),
    )
    artifact = Artifact(name="agent-sessions", storage=storage, lifecycle=lifecycle)

    result = artifact.prune(dry_run=False)
    assert result.pruned == 1
    assert not old_session.exists()
    assert active_session.exists()


# ---------------------------------------------------------------------------
# TransformAndDelete with SqliteRollup
# ---------------------------------------------------------------------------


def test_prune_sqlite_rollup_transform_and_delete(tmp_path: Path) -> None:
    """Rollup-shaped: transform_fn aggregates, then deletes originals."""
    db = tmp_path / "usage.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE turns (id INTEGER PRIMARY KEY, day TEXT, tokens INTEGER)"
        )
        conn.execute(
            "CREATE TABLE turns_daily (day TEXT, total_tokens INTEGER)"
        )
        conn.executemany(
            "INSERT INTO turns (day, tokens) VALUES (?, ?)",
            [
                ("2026-01-01", 100),
                ("2026-01-01", 200),
                ("2026-05-01", 50),
            ],
        )
        conn.commit()

    def fake_rollup(conn, *, dry_run: bool) -> dict:
        cursor = conn.execute(
            "SELECT day, SUM(tokens) FROM turns "
            "WHERE day < '2026-05-01' GROUP BY day"
        )
        rows = cursor.fetchall()
        if not dry_run:
            conn.executemany(
                "INSERT INTO turns_daily (day, total_tokens) VALUES (?, ?)",
                rows,
            )
            conn.execute("DELETE FROM turns WHERE day < '2026-05-01'")
            conn.commit()
        return {"rolled_turns": 2, "rollup_groups": 1}

    storage = SqliteRollupStorage(
        db_path=db, source_table="turns", rollup_table="turns_daily"
    )
    lifecycle = Lifecycle(
        trigger=TimeWindow(timestamp_field="day", window_days=30),
        action=TransformAndDelete(transform_fn=fake_rollup),
    )
    artifact = Artifact(name="usage", storage=storage, lifecycle=lifecycle)
    assert StorageTrait.TRANSFORM_ON_EXPIRY in artifact.capabilities

    result = artifact.prune(dry_run=False)
    assert result.pruned == 2
    assert result.transformed == 1
    with sqlite3.connect(str(db)) as conn:
        remaining = list(conn.execute("SELECT day FROM turns").fetchall())
        rolled = list(conn.execute("SELECT day, total_tokens FROM turns_daily"))
    assert len(remaining) == 1  # only 2026-05-01 row
    assert len(rolled) == 1
    assert rolled[0] == ("2026-01-01", 300)


# ---------------------------------------------------------------------------
# Provenance: list_by_session
# ---------------------------------------------------------------------------


def test_list_by_session_with_filesystem(tmp_path: Path) -> None:
    storage = FilesystemStorage(data_root=tmp_path)
    storage.save("a", "scratch", "rec-a", "txt", session_id="sess-1")
    storage.save("b", "scratch", "rec-b", "txt", session_id="sess-2")
    storage.save("c", "scratch", "rec-c", "txt", session_id="sess-1")

    lifecycle = Lifecycle(
        trigger=PerTypeTtl(ttl_days_by_type={"scratch": 3}),
        action=Delete(),
    )
    artifact = Artifact(
        name="filesystem",
        storage=storage,
        lifecycle=lifecycle,
        provenance=SessionTagged(session_field="session_id"),
    )
    refs = artifact.list_by_session("sess-1")
    assert len(refs) == 2
    assert all(r.artifact_name == "filesystem" for r in refs)


def test_session_tagged_columns_first_non_null_wins() -> None:
    prov = SessionTagged(session_columns=["sender_session", "recipient_session"])
    assert prov.get_session({"sender_session": "alice", "recipient_session": "bob"}) == "alice"
    assert prov.get_session({"sender_session": "", "recipient_session": "bob"}) == "bob"
    assert prov.get_session({"sender_session": None, "recipient_session": "bob"}) == "bob"
    assert prov.get_session({"other": "x"}) is None


# ---------------------------------------------------------------------------
# Registry sweep_all + dump
# ---------------------------------------------------------------------------


def test_registry_sweep_all_and_per_name(tmp_path: Path) -> None:
    """sweep_all iterates registered artifacts; name= scopes to one."""
    cache_path = tmp_path / "c.json"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    cache_path.write_text(json.dumps({"k1": {"expires_at": yesterday}}))

    storage = JsonRecordsStorage(path=cache_path, shape=JsonRecordsShape.DICT)
    lifecycle = Lifecycle(
        trigger=PerRecordTtl(ttl_field="expires_at"),
        action=Delete(),
    )
    artifact = Artifact(name="my-cache", storage=storage, lifecycle=lifecycle)
    register_artifact(artifact)

    results = sweep_all(dry_run=True)
    assert len(results) == 1
    assert results[0].artifact_name == "my-cache"
    assert results[0].pruned == 1

    # Per-name scope
    scoped = sweep_all(dry_run=True, name="my-cache")
    assert len(scoped) == 1
    assert scoped[0].artifact_name == "my-cache"

    # Unknown name returns an error result, not an exception
    unknown = sweep_all(dry_run=True, name="not-registered")
    assert len(unknown) == 1
    assert unknown[0].error is not None


def test_artifact_registry_dump_shape(tmp_path: Path) -> None:
    storage = JsonRecordsStorage(
        path=tmp_path / "x.json", shape=JsonRecordsShape.DICT
    )
    artifact = Artifact(
        name="dump-test",
        storage=storage,
        lifecycle=Lifecycle(
            trigger=PerRecordTtl(ttl_field="expires_at"),
            action=Delete(),
            retention_predicate=lambda r: r.get("keep") is True,
        ),
        exposed_operations=frozenset({Operation.CLEANUP}),
    )
    register_artifact(artifact)

    dumped = artifact_registry_dump()
    assert "dump-test" in dumped
    entry = dumped["dump-test"]
    assert entry["storage_kind"] == "JsonRecordsStorage"
    assert "PerRecordTtl" in entry["lifecycle_kind"]
    assert "Delete" in entry["lifecycle_kind"]
    assert "ConditionalRetention" in entry["lifecycle_kind"]
    assert "cleanup" in entry["exposed_operations"]
    assert "per_record_ttl" in entry["capabilities"]
    assert "conditional_retention" in entry["capabilities"]
