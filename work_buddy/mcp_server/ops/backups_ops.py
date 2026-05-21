"""Data-backup ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). These callables wrap
:mod:`work_buddy.backups` for invocation via the sidecar cron AND via the
user-facing slash commands (``/wb-backup-now``, ``/wb-backup-restore``).
"""

from __future__ import annotations

from pathlib import Path

from work_buddy.mcp_server.op_registry import register_op


def _last_run_ts(snapshot_id: str) -> str:
    """Normalise a snapshot id to a colon-delimited ISO-8601 timestamp.

    ``last_run.json``'s ``ts`` field feeds the ``github_backups``
    freshness health check, which parses it with
    ``datetime.fromisoformat``. The snapshot id's own time component
    uses dashes (``snap-2026-05-20T16-00-20Z``) and is not
    ISO-parseable, so it is converted here:
    ``snap-2026-05-20T16-00-20Z`` (or its ``-manual`` variant) →
    ``2026-05-20T16:00:20Z``.
    """
    from work_buddy.backups.local import parse_snapshot_ts

    dt = parse_snapshot_ts(snapshot_id)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else snapshot_id


def data_backup(manual: bool = False, push_remote: bool | None = None) -> dict:
    """Snapshot work-buddy's vital SQLite DBs; optionally push to remote."""
    from work_buddy.backups.local import run_backup
    from work_buddy.backups.remote import (
        get_backup_repo,
        prune_remote_snapshots,
        push_snapshot,
        write_last_run,
    )

    result = run_backup(manual=manual)
    snapshot_dir = Path(result["tarball_path"]).parent

    # Decide whether to push remote:
    # - If push_remote is explicitly True/False, honor it.
    # - Otherwise, push iff a backup repo is configured.
    if push_remote is None:
        push_remote = bool(get_backup_repo())

    if push_remote:
        push_result = push_snapshot(snapshot_dir)
        result["remote"] = push_result
        # Mirror the local retention on the remote.
        if push_result.get("status") == "ok":
            prune_result = prune_remote_snapshots()
            result["remote_pruned"] = prune_result.get("pruned", [])
        # Write last_run.json for the health check (regardless of whether the
        # push succeeded — failure-state visibility is exactly the point).
        write_last_run({
            "ts":          _last_run_ts(result["snapshot_id"]),
            "snapshot_id": result["snapshot_id"],
            "manual":      manual,
            "status":      "ok" if push_result.get("status") == "ok" else "error",
            "error":       push_result.get("error") if push_result.get("status") != "ok" else None,
            "remote":      push_result,
        })
    else:
        # Local-only run: still write last_run.json so the health check can
        # show "local-only mode" rather than "no backups".
        write_last_run({
            "ts":          _last_run_ts(result["snapshot_id"]),
            "snapshot_id": result["snapshot_id"],
            "manual":      manual,
            "status":      "ok",
            "remote":      {"status": "unconfigured"},
        })
    return result


def data_restore(
    snapshot_id: str,
    from_remote: bool = False,
    force: bool = False,
) -> dict:
    """Restore work-buddy's databases from a snapshot."""
    from work_buddy.backups.restore import restore

    return restore(snapshot_id, from_remote=from_remote, force=force)


def data_backup_list(include_remote: bool = False) -> dict:
    """List available local (and optionally remote) backup snapshots."""
    from work_buddy.backups.local import list_snapshots
    from work_buddy.backups.remote import list_remote_snapshots

    local = list_snapshots()
    # Strip the Manifest dataclass to a serializable form.
    local_serializable = []
    for s in local:
        mf = s.get("manifest")
        local_serializable.append({
            **{k: v for k, v in s.items() if k != "manifest"},
            "manifest": (
                {
                    "snapshot_ts": mf.snapshot_ts,
                    "work_buddy_commit": mf.work_buddy_commit,
                    "schema_versions": mf.schema_versions,
                } if mf else None
            ),
        })
    out: dict = {"local": local_serializable}
    if include_remote:
        out["remote"] = list_remote_snapshots()
    return out


def _register() -> None:
    register_op("op.wb.data_backup", data_backup)
    register_op("op.wb.data_restore", data_restore)
    register_op("op.wb.data_backup_list", data_backup_list)


_register()
