"""Data-backup ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). These callables wrap
:mod:`work_buddy.backups` for invocation via the sidecar cron AND via the
user-facing slash commands (``/wb-backup-now``, ``/wb-backup-restore``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from work_buddy.backups.remote import REMOTE_PRIVATE_CONTENT_UPLOAD_OPERATION
from work_buddy.consent import ConsentPrompt, requires_consent
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


def _remote_private_content_prompt(manual: bool, repo: str) -> ConsentPrompt:
    """Bind one remote upload approval to its destination and content class."""

    from work_buddy.backups.local import VITAL_DBS

    context = {
        "schema": "wb.remote-private-backup-consent/v1",
        "repo": repo,
        "manual": bool(manual),
        "archive_encryption": "none",
        "vital_databases": sorted(VITAL_DBS),
        "portable_truth_exports": True,
        "remote_retention_sweep": True,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ConsentPrompt(
        body=(
            "Upload one newly created, unencrypted Work Buddy backup to the "
            f"private GitHub Releases repository {repo}. The archive includes "
            "all vital databases (including Projects descriptions, Contracts, "
            "and Personal Knowledge) plus portable scoped Truth exports. A "
            "successful upload also applies the documented remote retention "
            "policy and may delete older out-of-policy backup releases."
        ),
        fingerprint=fingerprint,
        context=context,
    )


def _run_backup_with_remote_policy(
    *,
    manual: bool,
    push_remote: bool,
    repo: str | None,
    local_only_reason: str | None = None,
) -> dict:
    """Create the snapshot, then apply an already-decided remote policy."""

    from work_buddy.backups.local import run_backup
    from work_buddy.backups.remote import (
        prune_remote_snapshots,
        push_snapshot,
        write_last_run,
    )

    result = run_backup(manual=manual)
    snapshot_dir = Path(result["tarball_path"]).parent

    if push_remote:
        push_result = push_snapshot(snapshot_dir, repo=repo)
        result["remote"] = push_result
        # Mirror the local retention on the remote.
        if push_result.get("status") == "ok":
            prune_result = prune_remote_snapshots(repo=repo)
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
        remote_result = {
            "status": "local_only",
            "reason": local_only_reason or "remote_push_disabled",
        }
        result["remote"] = remote_result
        write_last_run({
            "ts":          _last_run_ts(result["snapshot_id"]),
            "snapshot_id": result["snapshot_id"],
            "manual":      manual,
            "status":      "ok",
            "remote":      remote_result,
        })
    return result


@requires_consent(
    operation=REMOTE_PRIVATE_CONTENT_UPLOAD_OPERATION,
    reason=(
        "Upload one unencrypted backup containing private domain data to the "
        "configured GitHub Releases repository."
    ),
    risk="high",
    consent_weight="high",
    default_ttl=0,
    grant_policy="per_invocation",
    request_factory=_remote_private_content_prompt,
)
def _run_one_shot_remote_backup(manual: bool, repo: str) -> dict:
    """Run the exact one-shot remote upload authorized by the user."""

    return _run_backup_with_remote_policy(
        manual=manual,
        push_remote=True,
        repo=repo,
    )


def data_backup(manual: bool = False, push_remote: bool | None = None) -> dict:
    """Snapshot vital DBs; keep private archives local unless authorized.

    ``push_remote=None`` is the scheduled/default path.  It uploads only when
    both a repository and the persistent private-content opt-in are present.
    An explicit ``push_remote=True`` without that opt-in requires one exact,
    high-risk, per-invocation consent decision before the snapshot is made.
    """

    from work_buddy.backups.remote import (
        get_backup_repo,
        remote_private_content_opted_in,
    )

    if push_remote is not None and not isinstance(push_remote, bool):
        raise TypeError("push_remote must be a boolean or null")

    repo = get_backup_repo()
    opted_in = remote_private_content_opted_in()

    if push_remote is True and repo and not opted_in:
        return _run_one_shot_remote_backup(bool(manual), repo)

    should_push = bool(repo) and opted_in and push_remote is not False
    if not repo:
        local_only_reason = "remote_repository_unconfigured"
    elif push_remote is False:
        local_only_reason = "remote_push_explicitly_disabled"
    elif not opted_in:
        local_only_reason = "private_content_opt_in_required"
    else:
        local_only_reason = None
    return _run_backup_with_remote_policy(
        manual=bool(manual),
        push_remote=should_push,
        repo=repo,
        local_only_reason=local_only_reason,
    )


@requires_consent(
    operation="backup.sensitive_checkpoint",
    reason=(
        "Create a local-only Journal snapshot beside an already authorized "
        "Sources export; the checkpoint contains private content."
    ),
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def data_sensitive_checkpoint(
    source_export_path: str,
    source_export_sha256: str,
    source_export_id: str,
    source_item_count: int,
    issued_copy_count: int,
    destination: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Seal Journal with an existing guarded Sources export, locally only."""

    from work_buddy.backups.sensitive import (
        AuthorizedSourceExport,
        create_sensitive_checkpoint_from_authorized_export,
        verify_sensitive_checkpoint,
    )
    from work_buddy.paths import data_dir, resolve

    source_path = Path(source_export_path).expanduser().resolve()
    root = (
        Path(destination).expanduser().resolve()
        if destination is not None
        else source_path.parent
    )
    backups_root = data_dir("backups").resolve()
    try:
        root.relative_to(backups_root)
    except ValueError as exc:
        raise ValueError("sensitive checkpoint must be under the backup root") from exc
    receipt = AuthorizedSourceExport(
        path=source_path,
        sha256=source_export_sha256,
        export_id=source_export_id,
        item_count=int(source_item_count),
        issued_copy_count=int(issued_copy_count),
    )
    result = create_sensitive_checkpoint_from_authorized_export(
        root,
        journal_db=resolve("db/journal-capture"),
        source_export=receipt,
        idempotency_key=idempotency_key or source_export_id,
    )
    # Re-read every member before returning success.  Results contain digests
    # and counts only; no Journal or Sources prose crosses the operator seam.
    return verify_sensitive_checkpoint(result.path).to_dict()


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
    register_op("op.wb.data_sensitive_checkpoint", data_sensitive_checkpoint)
    register_op("op.wb.data_restore", data_restore)
    register_op("op.wb.data_backup_list", data_backup_list)


_register()
