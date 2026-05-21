"""Local rolling-backup pipeline.

Each invocation produces a single tarball under
``<data_root>/backups/<isots>/work-buddy-backup.tar.gz`` containing:

- One ``<dbname>.db`` per vital SQLite DB, written via
  :py:meth:`sqlite3.Connection.backup` (hot-backup API — no writer
  blocking, consistent point-in-time snapshot, no WAL coherence
  issues).
- A ``MANIFEST.json`` with timestamp, git commit / branch / dirty
  flag, per-DB ``user_version``, per-table row counts. See
  :mod:`work_buddy.backups.manifest`.

The retention sweep runs after a successful backup, pruning out-of-
bucket snapshots per a tiered scheme:

  Hourly  ×24  (last 24 hours)
  Daily   ×7   (last week)
  Weekly  ×4   (last month)
  Monthly ×12  (last year)
  Annual  ×∞   (forever)
  Manual  ×20  (separate bucket; user-triggered ``/wb-backup-now``)

Manual snapshots (with ``-manual`` suffix on the directory name) are
pruned independently of the rolling scheme so a deliberate "about to
do something risky" snapshot doesn't get swept away by hourly churn.

No external dependencies — stdlib only. The pipeline is reusable for
either the cron-scheduled hourly tick OR the ``/wb-backup-now`` user
command; the only difference is the ``manual`` flag controlling the
filename suffix + retention bucket.

See ``architecture/backups`` for the full subsystem reference.
"""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.backups.manifest import (
    Manifest,
    build_manifest,
    read_manifest,
    utcnow_iso,
    write_manifest,
)
from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir, resolve

logger = get_logger(__name__)


# ─── Configuration ──────────────────────────────────────────────────


# Logical name → resource-id-in-paths.RESOURCES mapping. Adding a
# new vital DB means adding an entry here AND ensuring the resource
# is registered in work_buddy.paths.RESOURCES.
#
# The logical name is what appears as a key in the manifest's
# ``schema_versions`` and ``row_counts``. It does NOT have to match
# the on-disk filename — the tarball preserves the live basename
# (so restore can put the file straight back without a mapping
# table). Manifest <-> tarball mapping is reconstructed via this
# dict on the restore side.
VITAL_DBS: dict[str, str] = {
    "tasks":    "db/tasks",     # on-disk: task_metadata.db
    "projects": "db/projects",  # on-disk: projects.db
    "messages": "db/messages",  # on-disk: messages.db
    "threads":  "db/threads",   # on-disk: threads.db
    "entities": "db/entities",  # on-disk: entities.db
}


BACKUP_FILENAME = "work-buddy-backup.tar.gz"

# Manual-snapshot suffix on the snapshot dir name. Distinguishes
# user-triggered snapshots from cron snapshots so the retention
# sweep can keep them in a separate bucket.
MANUAL_SUFFIX = "-manual"


# Tiered retention. Order matters: each tier applies to the snapshots
# that survived all earlier tiers.
RETENTION = {
    "hourly":  24,
    "daily":   7,
    "weekly":  4,
    "monthly": 12,
    "annual":  -1,   # -1 = unbounded
    "manual":  20,   # separate bucket
}


# ─── Public API ─────────────────────────────────────────────────────


def run_backup(*, manual: bool = False) -> dict[str, Any]:
    """Take a snapshot of every vital DB, bundle into a tarball, prune.

    Returns a result dict suitable for capability output:

    .. code-block::

        {
          "status": "ok",
          "snapshot_id": "snap-2026-05-11T14-23-00Z",
          "tarball_path": "<data_root>/backups/snap-.../work-buddy-backup.tar.gz",
          "size_bytes": 3211284,
          "manifest": {...},
          "manual": bool,
          "pruned": [list of snapshot_ids deleted by the retention sweep],
        }

    Failures inside the snapshot get raised; the runner catches and
    logs. Best-effort: a single unreadable DB contributes a zero-byte
    backup file but doesn't break the whole snapshot.
    """
    ts = utcnow_iso()
    snapshot_id = f"snap-{ts}{MANUAL_SUFFIX if manual else ''}"
    backups_root = data_dir("backups")
    snapshot_dir = backups_root / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    logger.info("backup: starting snapshot %s", snapshot_id)

    # 1. Hot-backup every vital DB into a temp staging dir so we don't
    #    pollute the snapshot_dir if anything fails mid-flight.
    #    We preserve the LIVE FILENAME inside the tarball (not the
    #    logical name) so restore can drop the file straight back into
    #    .data/db/ without a name-mapping step.
    db_paths = _resolve_vital_dbs()
    with tempfile.TemporaryDirectory(
        prefix="work-buddy-backup-staging-",
    ) as staging_str:
        staging = Path(staging_str)
        for name, src in db_paths.items():
            dst = staging / src.name  # e.g. "task_metadata.db" for the "tasks" entry
            _hot_backup(src, dst)

        # 2. Build manifest by probing the LIVE DBs (not the
        #    staging copies) — they're identical in content but the
        #    live ones are where the canonical user_version + row
        #    counts come from. The probe is read-only.
        manifest = build_manifest(snapshot_ts=ts, db_paths=db_paths)
        write_manifest(manifest, staging)

        # 3. Tar+gzip the staging dir into the snapshot dir.
        tarball = snapshot_dir / BACKUP_FILENAME
        _make_tarball(staging, tarball)

    size_bytes = tarball.stat().st_size
    logger.info(
        "backup: snapshot %s complete (%.2f MB)",
        snapshot_id, size_bytes / 1_000_000,
    )

    # 4. Retention sweep — runs AFTER the new snapshot lands so we
    #    never prune the only copy.
    pruned = _prune_snapshots(backups_root)

    return {
        "status":       "ok",
        "snapshot_id":  snapshot_id,
        "tarball_path": str(tarball),
        "size_bytes":   size_bytes,
        "manifest":     manifest.to_json(),
        "manual":       manual,
        "pruned":       pruned,
    }


def list_snapshots(*, include_manual: bool = True) -> list[dict[str, Any]]:
    """List every local snapshot, newest first.

    Each entry: ``{snapshot_id, dir, ts, manual, size_bytes,
    manifest}``. ``manifest`` is the loaded :class:`Manifest` or
    ``None`` if the snapshot is malformed (missing manifest /
    unreadable JSON).
    """
    out: list[dict[str, Any]] = []
    backups_root = data_dir("backups")
    if not backups_root.exists():
        return out

    for entry in sorted(backups_root.iterdir(), reverse=True):
        if not entry.is_dir() or not entry.name.startswith("snap-"):
            continue
        manual = entry.name.endswith(MANUAL_SUFFIX)
        if manual and not include_manual:
            continue
        tarball = entry / BACKUP_FILENAME
        size_bytes = tarball.stat().st_size if tarball.exists() else 0
        try:
            # The manifest lives INSIDE the tarball, but we ALSO
            # write a copy at the snapshot_dir level for fast
            # listing without extracting the archive. If the
            # copy-at-snapshot-dir convention is added later, read
            # it here. For now read by extracting just the manifest
            # member.
            mf = _read_manifest_from_tarball(tarball)
        except Exception as exc:
            logger.warning(
                "list_snapshots: %s manifest unreadable: %s",
                entry.name, exc,
            )
            mf = None
        out.append({
            "snapshot_id": entry.name,
            "dir":         str(entry),
            "ts":          mf.snapshot_ts if mf else "",
            "manual":      manual,
            "size_bytes":  size_bytes,
            "manifest":    mf,
        })
    return out


# ─── Internals ──────────────────────────────────────────────────────


def _resolve_vital_dbs() -> dict[str, Path]:
    """Map logical DB name → live filesystem path. Unresolvable IDs
    are skipped with a warning (rather than failing the whole backup).
    """
    out: dict[str, Path] = {}
    for name, resource_id in VITAL_DBS.items():
        try:
            out[name] = resolve(resource_id)
        except KeyError as exc:
            logger.warning(
                "backup: vital DB %r unresolvable (resource %r missing "
                "from paths.RESOURCES): %s",
                name, resource_id, exc,
            )
    return out


def _hot_backup(src: Path, dst: Path) -> None:
    """SQLite hot-backup: page-by-page copy under lock protocol.

    Source DB may be live and actively written; destination is a
    fresh file. WAL pages get drained into the destination's main
    file during the copy.
    """
    if not src.exists():
        logger.warning("backup: source DB missing, skipping: %s", src)
        # Touch an empty file so the tarball still has a placeholder
        # — preserves the schema_versions key in the manifest.
        dst.touch()
        return
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _make_tarball(staging: Path, tarball: Path) -> None:
    """Tar+gzip every file under ``staging`` into ``tarball``.

    Members are stored with paths RELATIVE to ``staging`` so the
    extracted layout doesn't carry the absolute temp-dir name. The
    archive is reproducible enough for content-equality checks
    (gzip mtime is intentionally NOT zeroed — that requires
    reaching deeper into the gzip header — but the file ordering
    is deterministic).
    """
    tarball.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "w:gz") as tf:
        for entry in sorted(staging.iterdir()):
            tf.add(entry, arcname=entry.name)


def _read_manifest_from_tarball(tarball: Path) -> Manifest:
    """Extract ``MANIFEST.json`` from a snapshot tarball without
    unpacking the SQLite files."""
    with tarfile.open(tarball, "r:gz") as tf:
        member = tf.getmember("MANIFEST.json")
        fp = tf.extractfile(member)
        if fp is None:
            raise RuntimeError("MANIFEST.json is not a regular file in the tar")
        raw = fp.read().decode("utf-8")
    return Manifest.from_json(raw)


# ─── Retention ──────────────────────────────────────────────────────


def _prune_snapshots(backups_root: Path) -> list[str]:
    """Apply the tiered retention policy. Return the list of pruned
    snapshot IDs.

    Algorithm:

    1. Enumerate every snapshot directory; parse its timestamp.
    2. Split into rolling + manual buckets by suffix.
    3. For ROLLING:
       a. Sort by timestamp DESC.
       b. Walk newest-first, bucketing each snapshot into hourly /
          daily / weekly / monthly / annual based on whether it's
          the FIRST snapshot we see in that calendar window.
       c. Apply per-tier caps (oldest entries beyond the cap drop
          out of the retain set).
    4. For MANUAL: keep the newest ``RETENTION["manual"]``.
    5. Delete every snapshot dir not in the retain union.
    """
    all_snapshots = _enumerate_snapshots(backups_root)
    rolling = [s for s in all_snapshots if not s["manual"]]
    manual = [s for s in all_snapshots if s["manual"]]

    retain_rolling = _select_rolling_retain_set(rolling)
    retain_manual_ids = {s["id"] for s in manual[: RETENTION["manual"]]}

    keep_ids = retain_rolling | retain_manual_ids
    pruned: list[str] = []
    for s in all_snapshots:
        if s["id"] in keep_ids:
            continue
        try:
            shutil.rmtree(s["path"])
            pruned.append(s["id"])
            logger.info("backup: pruned %s", s["id"])
        except OSError as exc:
            logger.warning("backup: failed to prune %s: %s", s["id"], exc)
    return pruned


def _enumerate_snapshots(backups_root: Path) -> list[dict[str, Any]]:
    """Return snapshot dicts sorted newest-first by parsed timestamp."""
    out: list[dict[str, Any]] = []
    if not backups_root.exists():
        return out
    for entry in backups_root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("snap-"):
            continue
        ts = parse_snapshot_ts(entry.name)
        if ts is None:
            continue
        out.append({
            "id":     entry.name,
            "path":   entry,
            "ts":     ts,
            "manual": entry.name.endswith(MANUAL_SUFFIX),
        })
    out.sort(key=lambda s: s["ts"], reverse=True)
    return out


def parse_snapshot_ts(snapshot_id: str) -> datetime | None:
    """Snapshot dirs / release tags are named ``snap-<isots>[-manual]``.
    Extract the isots and parse it to a UTC datetime, or None if
    malformed.

    The ``isots`` is the *snapshot time* — the canonical timestamp the
    tiered retention sweep buckets on. Both the local sweep and the
    remote sweep parse it from this name; neither may substitute a
    GitHub release's ``createdAt`` (which is the tagged commit's date,
    constant across every release in a data-only repo)."""
    name = snapshot_id
    if name.endswith(MANUAL_SUFFIX):
        name = name[: -len(MANUAL_SUFFIX)]
    if not name.startswith("snap-"):
        return None
    ts_str = name[len("snap-"):]
    try:
        # utcnow_iso format: "2026-05-11T14-23-00Z"
        return datetime.strptime(ts_str, "%Y-%m-%dT%H-%M-%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _select_rolling_retain_set(rolling: list[dict[str, Any]]) -> set[str]:
    """Apply tiered retention to a list of rolling snapshots
    (sorted newest-first). Return the set of snapshot IDs to keep.

    The "bucket key" approach: each tier defines a key function that
    maps a timestamp to a bucket label. Within each tier, the first
    snapshot (chronologically newest) in each bucket is kept; the
    tier cap limits how many distinct buckets we honor.
    """
    if not rolling:
        return set()

    def hour_key(ts):    return ts.strftime("%Y-%m-%d-%H")
    def day_key(ts):     return ts.strftime("%Y-%m-%d")
    def week_key(ts):
        year, week, _ = ts.isocalendar()
        return f"{year}-W{week:02d}"
    def month_key(ts):   return ts.strftime("%Y-%m")
    def year_key(ts):    return ts.strftime("%Y")

    tiers = [
        ("hourly",  hour_key,  RETENTION["hourly"]),
        ("daily",   day_key,   RETENTION["daily"]),
        ("weekly",  week_key,  RETENTION["weekly"]),
        ("monthly", month_key, RETENTION["monthly"]),
        ("annual",  year_key,  RETENTION["annual"]),
    ]

    retain: set[str] = set()
    for tier_name, key_fn, cap in tiers:
        seen_buckets: list[str] = []
        for snap in rolling:
            bk = key_fn(snap["ts"])
            if bk in seen_buckets:
                continue
            # Bucket cap: -1 = unbounded
            if cap >= 0 and len(seen_buckets) >= cap:
                break
            seen_buckets.append(bk)
            retain.add(snap["id"])
    return retain
