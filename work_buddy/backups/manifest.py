"""Manifest format for backup snapshots.

A manifest is a small JSON file written alongside the SQLite backups
inside each snapshot directory. It captures everything a restore
operation needs to validate the snapshot before unpacking:

- Timestamp of the snapshot (ISO UTC).
- Git commit / branch / dirty flag of the work-buddy code that took
  the snapshot. Restore refuses if the snapshot's commit is newer
  than the running code — that would mean restoring a schema we
  don't know how to roll forward to.
- Per-DB ``PRAGMA user_version`` (the schema version known to the
  migration ladder). Restore refuses if any DB's version exceeds the
  highest migration this code knows about.
- Per-table row counts. Verified after restore + migrate as a sanity
  check that the unpacked DB has the data the snapshot recorded.
- Manifest format version (separate from work-buddy version) so we
  have a path forward if the manifest itself ever needs to change.

The manifest is small (~1-2 KB) and human-readable JSON. ``gh release``
asset users can download just the manifest to inspect a snapshot
without pulling the full tarball.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Bump when the manifest format itself changes (new fields are OK
# without bumping; structural changes require a bump + corresponding
# restore-side compatibility code).
MANIFEST_VERSION = 1


@dataclass
class Manifest:
    """Snapshot manifest.

    Serialized as JSON. Field names map directly to JSON keys.
    """

    snapshot_ts: str                              # ISO UTC, e.g. "2026-05-11T14:23:00Z"
    work_buddy_version: str | None                # from pyproject.toml (None if unreadable)
    work_buddy_commit: str | None                 # git rev-parse HEAD; None if not in a repo
    work_buddy_branch: str | None                 # git symbolic-ref; None if detached or not in a repo
    work_buddy_dirty: bool                        # True if working tree has uncommitted changes
    host: str                                     # socket.gethostname()
    schema_versions: dict[str, int] = field(default_factory=dict)
    # ^ {"task_metadata": 9, "projects": 0, "messages": 0, "threads": 0}
    #   Per-DB PRAGMA user_version. 0 for DBs without a migration ladder yet.

    row_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    # ^ {"task_metadata": {"task_metadata": 144, "task_action_items": 7, ...}, ...}
    #   Outer key is DB name; inner dict is per-table row counts within that DB.

    truth_stores: list[dict[str, Any]] = field(default_factory=list)
    # Each row reports one registered store and whether its portable recovery
    # payload was included under ``truth_stores/<store_id>/``.

    manifest_version: int = MANIFEST_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Manifest":
        data = json.loads(raw)
        # Accept manifests with unknown extra fields (forward-compat),
        # but require the structural fields we depend on.
        return cls(
            snapshot_ts        = data["snapshot_ts"],
            work_buddy_version = data.get("work_buddy_version"),
            work_buddy_commit  = data.get("work_buddy_commit"),
            work_buddy_branch  = data.get("work_buddy_branch"),
            work_buddy_dirty   = bool(data.get("work_buddy_dirty", False)),
            host               = data.get("host", ""),
            schema_versions    = dict(data.get("schema_versions", {})),
            row_counts         = {
                db: dict(tables)
                for db, tables in data.get("row_counts", {}).items()
            },
            truth_stores       = [
                dict(item) for item in data.get("truth_stores", [])
            ],
            manifest_version   = int(data.get("manifest_version", 1)),
        )


# ─── Builders ───────────────────────────────────────────────────────


def build_manifest(
    snapshot_ts: str,
    db_paths: dict[str, Path],
    repo_root: Path | None = None,
    truth_stores: list[dict[str, Any]] | None = None,
) -> Manifest:
    """Probe the given DBs + the git repo state to assemble a Manifest.

    Args:
        snapshot_ts: ISO UTC timestamp string for the snapshot.
        db_paths: Mapping of logical DB name (e.g. ``"task_metadata"``)
            to the live DB file. Each DB is opened READ-ONLY for the
            schema-version + row-count probes.
        repo_root: Path to the git repo whose commit / branch should
            be recorded. Defaults to the work-buddy repo root.

    Returns a fully-populated Manifest. Probes are best-effort; an
    unreadable DB contributes a 0 version + an empty row-count dict.
    """
    git_commit, git_branch, git_dirty = _probe_git(repo_root)
    schema_versions = {}
    row_counts: dict[str, dict[str, int]] = {}
    for name, path in db_paths.items():
        sv, counts = _probe_db(path)
        schema_versions[name] = sv
        row_counts[name] = counts
    return Manifest(
        snapshot_ts        = snapshot_ts,
        work_buddy_version = _probe_pyproject_version(repo_root),
        work_buddy_commit  = git_commit,
        work_buddy_branch  = git_branch,
        work_buddy_dirty   = git_dirty,
        host               = socket.gethostname(),
        schema_versions    = schema_versions,
        row_counts         = row_counts,
        truth_stores       = [dict(item) for item in (truth_stores or [])],
    )


# ─── Probes ─────────────────────────────────────────────────────────


def _probe_git(repo_root: Path | None) -> tuple[str | None, str | None, bool]:
    """Return ``(commit, branch, dirty)``. Each piece is best-effort.

    Uses ``git`` subprocess. If git isn't installed or the path isn't
    a repo, returns ``(None, None, False)``.
    """
    if repo_root is None:
        from work_buddy.paths import repo_root as _rr
        repo_root = _rr()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
        commit_sha = commit.stdout.strip() if commit.returncode == 0 else None
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.debug("manifest: git rev-parse failed: %s", exc)
        commit_sha = None
    try:
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
        branch_name = branch.stdout.strip() if branch.returncode == 0 else None
    except (subprocess.SubprocessError, FileNotFoundError):
        branch_name = None
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
    except (subprocess.SubprocessError, FileNotFoundError):
        dirty = False
    return commit_sha, branch_name, dirty


def _probe_pyproject_version(repo_root: Path | None) -> str | None:
    """Read ``pyproject.toml`` for the package version, if findable."""
    if repo_root is None:
        from work_buddy.paths import repo_root as _rr
        repo_root = _rr()
    pp = repo_root / "pyproject.toml"
    if not pp.exists():
        return None
    try:
        text = pp.read_text(encoding="utf-8")
    except OSError:
        return None
    # Lightweight extraction: look for `version = "x.y.z"` under
    # `[project]`. Avoid pulling tomllib for one field.
    import re
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def _probe_db(path: Path) -> tuple[int, dict[str, int]]:
    """Return ``(user_version, row_counts)`` for a single SQLite DB.

    Read-only. An unreadable DB contributes ``(0, {})``.
    """
    if not path.exists():
        return 0, {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        logger.debug("manifest: open %s failed: %s", path, exc)
        return 0, {}
    try:
        try:
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.OperationalError:
            user_version = 0
        # List every non-internal user table; count rows in each.
        try:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
        except sqlite3.OperationalError:
            tables = []
        counts: dict[str, int] = {}
        for t in tables:
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError as exc:
                logger.debug("manifest: count %s.%s failed: %s", path.name, t, exc)
        return int(user_version), counts
    finally:
        conn.close()


# ─── File IO ────────────────────────────────────────────────────────


MANIFEST_FILENAME = "MANIFEST.json"


def write_manifest(manifest: Manifest, snapshot_dir: Path) -> Path:
    """Write a manifest JSON into ``snapshot_dir/MANIFEST.json``."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / MANIFEST_FILENAME
    target.write_text(manifest.to_json(), encoding="utf-8")
    return target


def read_manifest(snapshot_dir: Path) -> Manifest:
    """Read a manifest JSON from ``snapshot_dir/MANIFEST.json``."""
    target = snapshot_dir / MANIFEST_FILENAME
    return Manifest.from_json(target.read_text(encoding="utf-8"))


def utcnow_iso() -> str:
    """Snapshot timestamps are recorded in compact ISO-UTC: ``2026-05-11T14-23-00Z``.

    Colons are replaced with dashes so the string is safe to use in
    filesystem paths (Windows rejects ``:`` in filenames) and GitHub
    release tags (which forbid most punctuation). The format is still
    sortable as ASCII.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
