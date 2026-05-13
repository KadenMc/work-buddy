"""``NeverExpires`` trigger and ``INFINITE_LIFECYCLE`` constant.

These pin the durable-retention contract that conversation_observability
(and any future user-authored or durable-state artifact) depends on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.artifacts import (
    Artifact,
    Capability,
    Delete,
    INFINITE_LIFECYCLE,
    Lifecycle,
    NeverExpires,
)


def test_never_expires_always_returns_false() -> None:
    trigger = NeverExpires()
    now = datetime.now(timezone.utc)

    assert trigger.is_expired({"created_at": "1990-01-01T00:00:00Z"}, now) is False
    assert trigger.is_expired({}, now) is False
    # A record that misrepresents itself as already-expired is still
    # retained — the trigger holds the policy authority, not the row.
    far_past = now - timedelta(days=10000)
    assert trigger.is_expired({"expires_at": far_past.isoformat()}, now) is False


def test_never_expires_advertises_no_trigger_capabilities() -> None:
    trigger = NeverExpires()
    # No trigger-side capability flag — the artifact's capability union
    # truthfully omits any expiry-policy advertisement.
    assert trigger.capabilities == frozenset()
    # In particular it does not falsely claim any of the other
    # trigger flavours.
    for cap in (
        Capability.PER_RECORD_TTL,
        Capability.PER_TYPE_TTL,
        Capability.TIME_WINDOW,
        Capability.MTIME_WINDOW,
    ):
        assert cap not in trigger.capabilities


def test_infinite_lifecycle_constant_uses_never_expires() -> None:
    assert isinstance(INFINITE_LIFECYCLE, Lifecycle)
    assert isinstance(INFINITE_LIFECYCLE.trigger, NeverExpires)
    assert isinstance(INFINITE_LIFECYCLE.action, Delete)


def test_infinite_lifecycle_find_expired_returns_empty(tmp_path) -> None:
    """An Artifact composed with INFINITE_LIFECYCLE never marks anything
    expired during a sweep, regardless of record timestamps.
    """
    import sqlite3

    from work_buddy.artifacts import SqliteRowsStorage

    db_path = tmp_path / "durable.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE things (id TEXT PRIMARY KEY, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO things (id, created_at) VALUES (?, ?)",
        [
            ("a", "1990-01-01T00:00:00Z"),
            ("b", "2000-01-01T00:00:00Z"),
            ("c", "2026-05-13T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()

    storage = SqliteRowsStorage(
        db_path=db_path,
        table="things",
        id_column="id",
    )
    artifact = Artifact(
        name="things-durable",
        storage=storage,
        lifecycle=INFINITE_LIFECYCLE,
    )

    result = artifact.prune(dry_run=True)
    assert result.pruned == 0
    assert result.error is None
    # Storage still has all three rows.
    rows = list(storage.iter_records())
    assert len(rows) == 3


def test_infinite_lifecycle_artifact_capabilities_omit_trigger_caps(
    tmp_path,
) -> None:
    """The union should not advertise a fake trigger capability."""
    from work_buddy.artifacts import SqliteRowsStorage

    storage = SqliteRowsStorage(
        db_path=tmp_path / "x.db",
        table="things",
        id_column="id",
    )
    # Schema must exist for list(); create empty table.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    conn.execute("CREATE TABLE things (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    artifact = Artifact(
        name="durable-empty",
        storage=storage,
        lifecycle=INFINITE_LIFECYCLE,
    )
    caps = artifact.capabilities
    # No trigger advertisements present.
    assert Capability.PER_RECORD_TTL not in caps
    assert Capability.PER_TYPE_TTL not in caps
    assert Capability.TIME_WINDOW not in caps
    assert Capability.MTIME_WINDOW not in caps
