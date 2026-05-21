"""Backup-timestamp normalisation + freshness-window regression guards.

``last_run.json``'s ``ts`` field is the contract between the backup op
(writer) and the ``github_backups`` health check (reader). Two failures
are pinned here:

1. The writer must emit a standard ISO-8601 timestamp. The snapshot id
   carries its time component with dashes (``...T16-00-20Z``), which is
   not ``fromisoformat``-parseable.
2. The reader must actually parse that timestamp and enforce the
   freshness window — a parse failure that silently degrades to
   "ok, window not enforced" hides a stalled backup indefinitely.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from work_buddy.health import checks
from work_buddy.mcp_server.ops.backups_ops import _last_run_ts


# ─── Writer: snapshot id → ISO-8601 ─────────────────────────────────


def test_last_run_ts_normalises_hourly_snapshot_id():
    assert _last_run_ts("snap-2026-05-20T16-00-20Z") == "2026-05-20T16:00:20Z"


def test_last_run_ts_normalises_manual_snapshot_id():
    # The `-manual` suffix must be stripped without eating real chars —
    # the str.rstrip("-manual") regression mangled any id whose tail
    # happened to be in that character set.
    assert (
        _last_run_ts("snap-2026-05-20T16-35-55Z-manual")
        == "2026-05-20T16:35:55Z"
    )


# ─── Reader: parse both timestamp shapes ────────────────────────────


def test_parse_backup_ts_accepts_standard_iso():
    dt = checks._parse_backup_ts("2026-05-20T16:00:20Z")
    assert dt == datetime(2026, 5, 20, 16, 0, 20, tzinfo=timezone.utc)


def test_parse_backup_ts_accepts_compact_snapshot_form():
    # An older last_run.json still carries the dashed time component.
    dt = checks._parse_backup_ts("2026-05-20T16-00-20Z")
    assert dt == datetime(2026, 5, 20, 16, 0, 20, tzinfo=timezone.utc)


def test_parse_backup_ts_rejects_garbage():
    assert checks._parse_backup_ts("not-a-timestamp") is None
    assert checks._parse_backup_ts("") is None


def test_writer_output_round_trips_through_reader():
    """The writer's output must be parseable by the reader — otherwise
    freshness silently stops being enforced."""
    ts = _last_run_ts("snap-2026-05-20T16-00-20Z")
    assert checks._parse_backup_ts(ts) is not None


# ─── Freshness window is actually enforced ──────────────────────────


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_freshness_passes_for_recent_successful_backup(monkeypatch):
    # check_github_backup_freshness imports both names inside the
    # function, so the source modules are the patch targets.
    fresh = _iso(datetime.now(timezone.utc) - timedelta(minutes=10))
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    monkeypatch.setattr(
        "work_buddy.backups.remote.read_last_run",
        lambda: {"status": "ok", "ts": fresh, "snapshot_id": "snap-x"},
    )
    res = checks.check_github_backup_freshness()
    assert res["ok"] is True


def test_freshness_fails_for_stale_successful_backup(monkeypatch):
    """A backup that succeeded once but has not run since must trip the
    window — the bug this guards against reported ok regardless of age
    because the timestamp never parsed."""
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=300))
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    monkeypatch.setattr(
        "work_buddy.backups.remote.read_last_run",
        lambda: {"status": "ok", "ts": stale, "snapshot_id": "snap-x"},
    )
    res = checks.check_github_backup_freshness()
    assert res["ok"] is False
    assert "old" in res["detail"]
