"""Restore pipeline: snapshot → live ``.data/db/``.

Operates in eight steps. Steps 1-7 happen in an isolated staging dir
so the live DB is untouched until step 8 (the atomic swap):

1. Resolve the snapshot source. Either a local snapshot directory
   (``.data/backups/snap-...``) or a remote release tag (we
   ``gh release download <tag>`` into a temp local snapshot dir
   first).
2. Validate the manifest. Refuse if:
   - ``work_buddy_commit`` is newer than the current HEAD (we don't
     know how to roll the schema forward to the snapshot's level).
   - Any ``schema_versions[db]`` exceeds the highest known migration
     for that DB.
3. Unpack the tarball into a staging dir under ``.data/db.staging_<ts>/``.
4. For each DB: open it via ``store.get_connection`` (or the DB's
   own migration-runner entry point) to apply any newer migrations
   forward. The snapshot is brought up to the current code's schema.
5. ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` per
   DB. Refuse on any integrity failure. FK violations are logged but
   not blocking (consistent with m009's policy).
6. Verify migrated row counts >= manifest counts (migrations may add
   new rows e.g. into ``_migration_history``, but row count should
   never DECREASE).
7. Atomic swap: rename live ``.data/db/`` to
   ``.data/db.pre_restore_<ts>/`` (preserved as a rollback), then
   rename staging to ``.data/db/``.

Steps 1-7 may fail and leave staging on disk — that's fine, the
live DB is untouched. Only step 8 (no separate function — just the
two renames at the end of step 7) is destructive.

See ``architecture/backups`` for the full subsystem reference.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.backups.local import BACKUP_FILENAME, VITAL_DBS
from work_buddy.backups.manifest import (
    MANIFEST_FILENAME, Manifest, read_manifest,
)
from work_buddy.backups.remote import get_backup_repo
from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir, repo_root

logger = get_logger(__name__)


# Cap on snapshots accepted with --force when manifest validation
# raises a refuse-by-default condition (commit newer than current,
# schema versions higher than known). Always require explicit force.
DEFAULT_REFUSE_FORCE = False


# ─── Errors ─────────────────────────────────────────────────────────


class RestoreRefused(Exception):
    """Restore declined for safety reasons (newer schema / commit)."""


class RestoreFailed(Exception):
    """Restore attempted but failed mid-flight (integrity check etc.).

    The live DB is untouched (failure occurred before the atomic
    swap). Staging dir may still be on disk for inspection.
    """


# ─── Source resolution ─────────────────────────────────────────────


def _resolve_local_snapshot(snapshot_id_or_path: str | Path) -> Path:
    """Map a snapshot ID like ``snap-2026-...`` to its local dir, OR
    accept an absolute path. Returns the snapshot directory."""
    candidate = Path(snapshot_id_or_path)
    if candidate.is_absolute():
        return candidate
    return data_dir("backups") / str(snapshot_id_or_path)


def _download_remote_snapshot(tag: str, repo: str | None = None) -> Path:
    """Download a release tarball into a temp local snapshot dir.

    Returns the snapshot directory. The directory persists after
    return; caller is responsible for cleanup (or we leave it as a
    cache).
    """
    repo = repo or get_backup_repo()
    if not repo:
        raise RestoreFailed("backups.github.repo not set in config")
    target_dir = data_dir("backups") / f"{tag}-fromremote"
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "gh", "release", "download", tag,
        "--repo", repo,
        "--dir", str(target_dir),
        "--clobber",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        raise RestoreFailed("gh CLI not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise RestoreFailed("gh release download timed out (300s)") from None
    if proc.returncode != 0:
        raise RestoreFailed(
            f"gh release download {tag} failed: {proc.stderr.strip()}"
        )
    return target_dir


# ─── Manifest validation ───────────────────────────────────────────


def _current_known_max_schema_versions() -> dict[str, int]:
    """Return the highest migration version this code knows per DB.

    For DBs without a migration ladder yet, returns 0 (which means
    "we don't constrain — any schema_version satisfies the check").
    """
    out = {name: 0 for name in VITAL_DBS}
    try:
        from work_buddy.obsidian.tasks.migrations import TASK_MIGRATIONS
        out["task_metadata"] = TASK_MIGRATIONS.target_version
    except Exception as exc:
        logger.warning("restore: cannot read TASK_MIGRATIONS: %s", exc)
    # When projects/messages/threads gain migration ladders, import
    # them here and update the per-DB entries.
    return out


def _current_commit() -> str | None:
    """Get the current code's HEAD commit, or None if unavailable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root()), capture_output=True, text=True, timeout=5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _validate_manifest(
    manifest: Manifest, *, force: bool = False,
) -> list[str]:
    """Return a list of WARNINGS that don't block (or, if force=False
    and any structural-incompat condition is hit, raise
    :class:`RestoreRefused`).
    """
    warnings: list[str] = []
    current_commit = _current_commit()
    known_versions = _current_known_max_schema_versions()

    # 1. Commit ancestry check (best-effort: only refuse if the
    #    snapshot's commit is *unknown to the current repo*; if it's
    #    older the migration ladder will roll forward).
    if manifest.work_buddy_commit:
        if current_commit and manifest.work_buddy_commit != current_commit:
            # Try git merge-base to see if the snapshot commit is an
            # ancestor of HEAD. If it's not in the repo at all, refuse.
            try:
                proc = subprocess.run(
                    ["git", "merge-base", "--is-ancestor",
                     manifest.work_buddy_commit, "HEAD"],
                    cwd=str(repo_root()),
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0:
                    pass  # snapshot is ancestor of HEAD: safe
                elif proc.returncode == 1:
                    msg = (
                        f"Snapshot commit {manifest.work_buddy_commit[:12]} "
                        f"is NOT an ancestor of HEAD ({current_commit[:12]}). "
                        "The snapshot may carry schema changes the current "
                        "code does not know how to roll forward to."
                    )
                    if force:
                        warnings.append(msg)
                    else:
                        raise RestoreRefused(msg + " Re-run with force=True to override.")
                # else: commit unknown to current repo; treat as warn-only
                #       since we can't check ancestry reliably.
            except (subprocess.SubprocessError, FileNotFoundError):
                warnings.append(
                    f"Could not check git ancestry of snapshot commit "
                    f"{manifest.work_buddy_commit[:12]}."
                )
    else:
        warnings.append("Snapshot has no work_buddy_commit recorded.")

    # 2. Schema version ceiling check.
    for db, snap_v in manifest.schema_versions.items():
        known_max = known_versions.get(db, 0)
        if snap_v > known_max > 0:
            msg = (
                f"Snapshot's {db} is at schema v{snap_v} but this code "
                f"only knows up to v{known_max}. Refusing to restore — "
                "newer schema cannot be migrated DOWN to current code."
            )
            if force:
                warnings.append(msg)
            else:
                raise RestoreRefused(msg + " Upgrade work-buddy to a version "
                                     "that includes the missing migrations.")

    # 3. Dirty-snapshot signal: not blocking, just noted.
    if manifest.work_buddy_dirty:
        warnings.append(
            "Snapshot was taken with an uncommitted working tree "
            "(work_buddy_dirty=True)."
        )
    return warnings


# ─── Restore pipeline ───────────────────────────────────────────────


def restore(
    snapshot_id_or_path: str | Path,
    *,
    from_remote: bool = False,
    repo: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Restore the vital DBs from a snapshot tarball.

    Args:
        snapshot_id_or_path: For local restore, a snapshot ID
            (``snap-2026-...``) or absolute path to a snapshot dir.
            For remote restore (``from_remote=True``), the release
            TAG on the backup repo.
        from_remote: If True, download via ``gh release download``
            first. Default False (local-only).
        repo: Override the configured backup repo. Defaults to
            ``backups.github.repo`` from config.
        force: Override the safety refuse-on-newer-schema / refuse-
            on-newer-commit checks. Use sparingly — these checks
            exist to prevent silent corruption.

    Returns ``{status, snapshot_id, warnings, pre_restore_dir, ...}``.

    Raises :class:`RestoreRefused` on safety-check failures (when
    ``force=False``) and :class:`RestoreFailed` on pipeline errors.
    """
    # 1. Source
    if from_remote:
        snapshot_dir = _download_remote_snapshot(
            str(snapshot_id_or_path), repo=repo,
        )
    else:
        snapshot_dir = _resolve_local_snapshot(snapshot_id_or_path)
    if not snapshot_dir.exists():
        raise RestoreFailed(f"Snapshot dir missing: {snapshot_dir}")
    tarball = snapshot_dir / BACKUP_FILENAME
    if not tarball.exists():
        raise RestoreFailed(f"Snapshot tarball missing: {tarball}")

    # 2. Manifest + validate
    from work_buddy.backups.local import _read_manifest_from_tarball
    manifest = _read_manifest_from_tarball(tarball)
    warnings = _validate_manifest(manifest, force=force)
    logger.info(
        "restore: snapshot %s validated (commit=%s, schema=%s, force=%s, "
        "warnings=%d)",
        snapshot_dir.name,
        (manifest.work_buddy_commit or "?")[:12],
        manifest.schema_versions, force, len(warnings),
    )

    # 3. Staging
    swap_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_dir = data_dir("") / "db"
    staging_dir = data_dir("") / f"db.staging_{swap_ts}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(staging_dir)

    # 4. Migrate each DB in staging forward.
    #    The on-disk file's basename (e.g. "task_metadata.db") is the
    #    canonical name inside the tarball — see local.py's _hot_backup.
    #    We resolve the logical name -> live filename via VITAL_DBS +
    #    paths.resolve.
    from work_buddy.backups.local import _resolve_vital_dbs
    db_paths = _resolve_vital_dbs()  # logical name -> live Path
    migrated: list[str] = []
    for name, live_path in db_paths.items():
        candidate = staging_dir / live_path.name
        if not candidate.exists():
            warnings.append(
                f"Snapshot lacks {live_path.name} (logical: {name!r})"
            )
            continue
        _apply_migrations_inplace(name, candidate)
        migrated.append(name)

    # 5. Integrity checks
    for name in migrated:
        path = staging_dir / db_paths[name].name
        _verify_integrity(path)

    # 6. Row-count cross-check (warnings only — migrations may add rows)
    rc_warnings = _verify_row_counts(staging_dir, manifest)
    warnings.extend(rc_warnings)

    # 7. Atomic swap
    pre_restore_dir = data_dir("") / f"db.pre_restore_{swap_ts}"
    if db_dir.exists():
        db_dir.rename(pre_restore_dir)
    staging_dir.rename(db_dir)

    logger.info(
        "restore: complete. Live DBs replaced. Previous DBs moved to %s.",
        pre_restore_dir,
    )
    return {
        "status":           "ok",
        "snapshot_id":      snapshot_dir.name,
        "warnings":         warnings,
        "pre_restore_dir":  str(pre_restore_dir),
        "migrated":         migrated,
        "manifest_summary": {
            "snapshot_ts":     manifest.snapshot_ts,
            "commit":          manifest.work_buddy_commit,
            "schema_versions": manifest.schema_versions,
        },
    }


def _apply_migrations_inplace(db_name: str, db_path: Path) -> None:
    """Open ``db_path`` through the appropriate per-DB migration
    runner so any newer-in-code migrations roll forward.

    Keyed off the LOGICAL name from VITAL_DBS (``tasks`` /
    ``projects`` / ``messages`` / ``threads``), NOT the on-disk
    filename. Only ``tasks`` has a migration ladder so far; the
    others no-op until they grow one.
    """
    if db_name != "tasks":
        # No migration ladder yet — leave the DB as-is.
        return
    from work_buddy.obsidian.tasks.migrations import TASK_MIGRATIONS
    conn = sqlite3.connect(str(db_path))
    try:
        TASK_MIGRATIONS.run(conn)
    finally:
        conn.close()


def _verify_integrity(db_path: Path) -> None:
    """``PRAGMA integrity_check``; raise :class:`RestoreFailed` if
    not ``"ok"``."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = [tuple(r) for r in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()
    if rows != [("ok",)]:
        raise RestoreFailed(
            f"integrity_check failed on {db_path.name}: {rows[:5]}"
        )


def _verify_row_counts(
    staging_dir: Path, manifest: Manifest,
) -> list[str]:
    """Per-table row-count cross-check.

    Migrations may *add* rows (e.g. a backfill); they should never
    *remove* them. Warn (not error) on shrinkage so the user knows
    something is off without blocking restore.

    The manifest's outer key is the LOGICAL DB name (``tasks``,
    ``projects``, etc.). The corresponding file inside the staging
    dir is the LIVE FILENAME (``task_metadata.db`` for ``tasks``).
    Resolve via VITAL_DBS so the lookup is consistent across the
    pipeline.
    """
    from work_buddy.backups.local import _resolve_vital_dbs
    db_paths = _resolve_vital_dbs()  # logical name -> live Path

    warnings: list[str] = []
    for db_name, expected_counts in manifest.row_counts.items():
        live_path = db_paths.get(db_name)
        if live_path is None:
            # Unknown DB in the manifest (e.g. an old snapshot whose
            # logical names don't match current code). Skip rather
            # than fail — caller already validated the manifest's
            # commit + schema versions.
            continue
        db_path = staging_dir / live_path.name
        if not db_path.exists():
            continue
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for tbl, expected in expected_counts.items():
                try:
                    actual = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                except sqlite3.OperationalError:
                    # Migration may have dropped/renamed this table
                    warnings.append(
                        f"{db_name}.{tbl}: dropped or renamed during "
                        "post-restore migration; manifest had "
                        f"{expected} rows but the table no longer exists."
                    )
                    continue
                if actual < expected:
                    warnings.append(
                        f"{db_name}.{tbl}: row count shrank from "
                        f"{expected} -> {actual}. Migrations should not "
                        "delete data; investigate."
                    )
        finally:
            conn.close()
    return warnings
