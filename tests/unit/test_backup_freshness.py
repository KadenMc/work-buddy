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


# ─── Runs that stayed on this computer ──────────────────────────────


def _local_only_run(minutes_ago: int, last_upload: dict | None = None) -> dict:
    run = {
        "status": "ok",
        "ts": _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)),
        "snapshot_id": "snap-local",
        "remote": {
            "status": "local_only",
            "reason": "private_content_opt_in_required",
        },
    }
    if last_upload is not None:
        run["last_upload"] = last_upload
    return run


def _check_with(monkeypatch, last_run: dict) -> dict:
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    monkeypatch.setattr("work_buddy.backups.remote.read_last_run", lambda: last_run)
    return checks.check_github_backup_freshness()


def test_fresh_local_only_run_is_not_a_github_backup(monkeypatch):
    """A snapshot that never left this computer used to report "last
    backup ok", which hid weeks of backups that never reached GitHub."""
    res = _check_with(monkeypatch, _local_only_run(minutes_ago=5))
    assert res["ok"] is False
    assert "staying on this computer" in res["detail"]
    assert "allow_unencrypted_private_content" in res["detail"]
    assert "No upload to GitHub is recorded" in res["detail"]


def test_local_only_run_passes_while_last_upload_is_fresh(monkeypatch):
    upload = {
        "ts": _iso(datetime.now(timezone.utc) - timedelta(minutes=30)),
        "snapshot_id": "snap-uploaded",
    }
    res = _check_with(monkeypatch, _local_only_run(minutes_ago=5, last_upload=upload))
    assert res["ok"] is True
    assert "snap-uploaded" in res["detail"]
    assert "stayed on this computer" in res["detail"]


def test_local_only_run_fails_once_last_upload_is_stale(monkeypatch):
    upload = {"ts": "2026-08-28T14:00:00Z", "snapshot_id": "snap-old"}
    res = _check_with(monkeypatch, _local_only_run(minutes_ago=5, last_upload=upload))
    assert res["ok"] is False
    assert "2026-08-28 14:00 UTC" in res["detail"]


# ─── Writer: the last upload is carried forward ─────────────────────


def test_carried_last_upload_prefers_the_recorded_marker():
    from work_buddy.mcp_server.ops.backups_ops import _carried_last_upload

    previous = _local_only_run(
        minutes_ago=5,
        last_upload={"ts": "2026-10-06T21:00:00Z", "snapshot_id": "snap-up"},
    )
    assert _carried_last_upload(previous) == {
        "ts": "2026-10-06T21:00:00Z", "snapshot_id": "snap-up",
    }


def test_carried_last_upload_reads_an_older_file_that_uploaded():
    from work_buddy.mcp_server.ops.backups_ops import _carried_last_upload

    previous = {
        "status": "ok",
        "ts": "2026-08-28T14:00:00Z",
        "snapshot_id": "snap-pushed",
        "remote": {"status": "ok"},
    }
    assert _carried_last_upload(previous) == {
        "ts": "2026-08-28T14:00:00Z", "snapshot_id": "snap-pushed",
    }


def test_carried_last_upload_is_none_without_an_upload():
    from work_buddy.mcp_server.ops.backups_ops import _carried_last_upload

    assert _carried_last_upload(None) is None
    assert _carried_last_upload(_local_only_run(minutes_ago=5)) is None
    assert _carried_last_upload(
        {"status": "error", "ts": "2026-10-06T21:00:00Z", "remote": {"status": "gh_network"}},
    ) is None


def _run_policy(monkeypatch, *, push_remote: bool, previous: dict | None,
                push_status: str = "ok") -> dict:
    """Run the backup policy with the snapshot and GitHub calls stubbed."""
    from work_buddy.mcp_server.ops import backups_ops

    written: list[dict] = []
    monkeypatch.setattr(
        "work_buddy.backups.local.run_backup",
        lambda manual: {
            "snapshot_id": "snap-2026-10-06T22-00-00Z",
            "tarball_path": "/tmp/snap/backup.tar.gz",
        },
    )
    monkeypatch.setattr(
        "work_buddy.backups.remote.push_snapshot",
        lambda snapshot_dir, repo: {"status": push_status},
    )
    monkeypatch.setattr(
        "work_buddy.backups.remote.prune_remote_snapshots",
        lambda repo: {"pruned": []},
    )
    monkeypatch.setattr("work_buddy.backups.remote.read_last_run", lambda: previous)
    monkeypatch.setattr("work_buddy.backups.remote.write_last_run", written.append)
    backups_ops._run_backup_with_remote_policy(
        manual=False,
        push_remote=push_remote,
        repo="owner/backups",
        local_only_reason=None if push_remote else "private_content_opt_in_required",
    )
    assert len(written) == 1
    return written[0]


def test_local_only_run_keeps_the_previous_upload(monkeypatch):
    previous = {
        "status": "ok",
        "ts": "2026-10-06T21:00:00Z",
        "snapshot_id": "snap-up",
        "remote": {"status": "ok"},
    }
    payload = _run_policy(monkeypatch, push_remote=False, previous=previous)
    assert payload["remote"]["status"] == "local_only"
    assert payload["last_upload"] == {
        "ts": "2026-10-06T21:00:00Z", "snapshot_id": "snap-up",
    }


def test_successful_upload_becomes_the_last_upload(monkeypatch):
    payload = _run_policy(monkeypatch, push_remote=True, previous=None)
    assert payload["last_upload"] == {
        "ts": "2026-10-06T22:00:00Z",
        "snapshot_id": "snap-2026-10-06T22-00-00Z",
    }


def test_failed_upload_keeps_the_previous_upload(monkeypatch):
    previous = _local_only_run(
        minutes_ago=5,
        last_upload={"ts": "2026-10-06T21:00:00Z", "snapshot_id": "snap-up"},
    )
    payload = _run_policy(
        monkeypatch, push_remote=True, previous=previous, push_status="gh_network",
    )
    assert payload["status"] == "error"
    assert payload["last_upload"] == {
        "ts": "2026-10-06T21:00:00Z", "snapshot_id": "snap-up",
    }
