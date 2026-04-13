"""Centralized artifact management for work-buddy.

Every agent-produced file — context bundles, exports, reports, snapshots,
scratch work — goes through this module.  Artifacts are stored globally
under ``data/<type>/`` with per-file metadata, session provenance, and
TTL-based automatic cleanup.

Usage (module-level convenience)::

    from work_buddy import artifacts

    rec = artifacts.save("report content", "report", "weekly-review", "md",
                         tags=["weekly"], description="Week 15 review")
    items = artifacts.list_artifacts(type="report")
    rec = artifacts.get("20260412-093000_weekly-review")

Or via MCP::

    mcp__work-buddy__wb_run("artifact_save", {
        "content": "...", "type": "report", "slug": "weekly-review", ...
    })
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.paths import data_dir

# ---------------------------------------------------------------------------
# Artifact types and their default TTL (days)
# ---------------------------------------------------------------------------

ARTIFACT_TYPES: dict[str, int] = {
    "context": 7,
    "export": 90,
    "report": 30,
    "snapshot": 14,
    "scratch": 3,
    "commit": 90,
}

_DEFAULT_TTL_DAYS = 14  # for unregistered types

_SLUG_RE = re.compile(r"[^a-z0-9_-]")
_TS_FMT = "%Y%m%d-%H%M%S"


# ---------------------------------------------------------------------------
# ArtifactRecord
# ---------------------------------------------------------------------------


@dataclass
class ArtifactRecord:
    """Immutable data container for a single artifact's identity + metadata."""

    id: str
    path: Path
    meta_path: Path
    type: str
    slug: str
    ext: str
    created_at: datetime
    session_id: str | None
    tags: list[str]
    description: str
    size_bytes: int
    ttl_days: int
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = self.path.as_posix()
        d["meta_path"] = self.meta_path.as_posix()
        d["created_at"] = self.created_at.isoformat()
        d["expires_at"] = self.expires_at.isoformat()
        d["is_expired"] = self.is_expired
        return d

    @classmethod
    def from_meta(cls, meta_path: Path) -> ArtifactRecord:
        """Reconstruct a record from a ``.meta.json`` file."""
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        content_path = meta_path.with_name(
            meta_path.name.replace(".meta.json", f".{raw['ext']}")
        )
        return cls(
            id=raw["id"],
            path=content_path,
            meta_path=meta_path,
            type=raw["type"],
            slug=raw.get("slug", ""),
            ext=raw.get("ext", "json"),
            created_at=datetime.fromisoformat(raw["created_at"]),
            session_id=raw.get("session_id"),
            tags=raw.get("tags", []),
            description=raw.get("description", ""),
            size_bytes=raw.get("size_bytes", 0),
            ttl_days=raw.get("ttl_days", _DEFAULT_TTL_DAYS),
            expires_at=datetime.fromisoformat(raw["expires_at"]),
        )


# ---------------------------------------------------------------------------
# ArtifactStore
# ---------------------------------------------------------------------------


class ArtifactStore:
    """Central artifact storage manager.

    Parameters
    ----------
    data_root:
        Override the data directory root.  ``None`` reads from config
        (``paths.data_root``, default ``<repo>/data``).
    """

    def __init__(self, data_root: Path | None = None) -> None:
        if data_root is not None:
            data_root.mkdir(parents=True, exist_ok=True)
            self._root = data_root
        else:
            self._root = data_dir()  # creates data/ if needed

    # ------------------------------------------------------------------ CRUD

    def save(
        self,
        content: str | bytes,
        type: str,
        slug: str,
        ext: str = "json",
        *,
        tags: list[str] | None = None,
        description: str = "",
        session_id: str | None = None,
        ttl_days: int | None = None,
    ) -> ArtifactRecord:
        """Atomically write an artifact + sidecar metadata.

        Returns the newly created :class:`ArtifactRecord`.
        """
        safe_slug = _SLUG_RE.sub("-", slug.lower().strip())[:80]
        now = datetime.now(timezone.utc)
        ts = now.strftime(_TS_FMT)
        artifact_id = f"{ts}_{safe_slug}"

        ttl = self._resolve_ttl(type, ttl_days)
        expires = now + timedelta(days=ttl)

        type_dir = self._type_dir(type)
        content_path = type_dir / f"{artifact_id}.{ext}"
        meta_path = type_dir / f"{artifact_id}.meta.json"

        # Resolve session dir name for provenance
        session_dir_name = self._session_dir_name(session_id)

        # Write content atomically
        content_bytes = (
            content.encode("utf-8") if isinstance(content, str) else content
        )
        self._atomic_write(content_path, content_bytes)

        # Write metadata
        meta: dict[str, Any] = {
            "id": artifact_id,
            "type": type,
            "slug": safe_slug,
            "ext": ext,
            "created_at": now.isoformat(),
            "session_id": session_id,
            "session_dir": session_dir_name,
            "tags": tags or [],
            "description": description,
            "size_bytes": len(content_bytes),
            "ttl_days": ttl,
            "expires_at": expires.isoformat(),
        }
        self._atomic_write(
            meta_path, json.dumps(meta, indent=2).encode("utf-8")
        )

        record = ArtifactRecord(
            id=artifact_id,
            path=content_path,
            meta_path=meta_path,
            type=type,
            slug=safe_slug,
            ext=ext,
            created_at=now,
            session_id=session_id,
            tags=tags or [],
            description=description,
            size_bytes=len(content_bytes),
            ttl_days=ttl,
            expires_at=expires,
        )

        # Record to session ledger if session_id is available
        if session_id:
            self.record_to_session_ledger(session_id, artifact_id)

        return record

    def get(self, artifact_id: str) -> ArtifactRecord:
        """Lookup by ID (filename stem).  Raises ``FileNotFoundError``."""
        meta_path = self._find_meta(artifact_id)
        if meta_path is None:
            raise FileNotFoundError(
                f"No artifact found with id '{artifact_id}'"
            )
        return ArtifactRecord.from_meta(meta_path)

    def read_content(self, artifact_id: str) -> str:
        """Return the text content of an artifact."""
        rec = self.get(artifact_id)
        return rec.path.read_text(encoding="utf-8")

    def delete(self, artifact_id: str) -> bool:
        """Delete content + meta files.  Returns ``True`` if found."""
        meta_path = self._find_meta(artifact_id)
        if meta_path is None:
            return False
        rec = ArtifactRecord.from_meta(meta_path)
        if rec.path.exists():
            rec.path.unlink()
        if rec.meta_path.exists():
            rec.meta_path.unlink()
        return True

    # --------------------------------------------------------------- Query

    def list(
        self,
        type: str | None = None,
        since: datetime | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        include_expired: bool = False,
        limit: int = 50,
    ) -> list[ArtifactRecord]:
        """Scan ``.meta.json`` files, filter, sort by created_at desc."""
        results: list[ArtifactRecord] = []

        dirs_to_scan: list[Path] = []
        if type:
            td = self._root / type
            if td.is_dir():
                dirs_to_scan.append(td)
        else:
            for entry in self._root.iterdir():
                if entry.is_dir() and not entry.name.startswith("_"):
                    dirs_to_scan.append(entry)

        for d in dirs_to_scan:
            for meta_file in d.glob("*.meta.json"):
                try:
                    rec = ArtifactRecord.from_meta(meta_file)
                except (json.JSONDecodeError, KeyError, OSError):
                    continue

                if not include_expired and rec.is_expired:
                    continue
                if since and rec.created_at < since:
                    continue
                if tags and not set(tags).issubset(set(rec.tags)):
                    continue
                if session_id and rec.session_id != session_id:
                    continue

                results.append(rec)

        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[:limit]

    # ----------------------------------------------------------- Lifecycle

    def cleanup(self, dry_run: bool = True) -> dict[str, Any]:
        """Unified cleanup: file-level TTL sweep + entry-level pruning.

        1. Delete expired timestamped artifacts (file + meta).
        2. Run registered pruners on singleton resources (chrome ledger,
           LLM cache, etc.) to trim stale entries within long-lived files.

        Returns a summary of both phases.
        """
        now = datetime.now(timezone.utc)

        # --- Phase 1: file-level TTL sweep ---
        deleted: list[dict[str, Any]] = []
        bytes_freed = 0

        for d in self._root.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            for meta_file in d.glob("*.meta.json"):
                try:
                    rec = ArtifactRecord.from_meta(meta_file)
                except (json.JSONDecodeError, KeyError, OSError):
                    continue
                if now >= rec.expires_at:
                    deleted.append(
                        {"id": rec.id, "type": rec.type, "size_bytes": rec.size_bytes}
                    )
                    bytes_freed += rec.size_bytes
                    if not dry_run:
                        if rec.path.exists():
                            rec.path.unlink()
                        if rec.meta_path.exists():
                            rec.meta_path.unlink()

        # --- Phase 2: entry-level pruning of singleton resources ---
        pruner_results = _run_pruners(dry_run=dry_run)

        return {
            "dry_run": dry_run,
            "artifacts_deleted": len(deleted),
            "artifacts_bytes_freed": bytes_freed,
            "deleted": deleted,
            "pruners": pruner_results,
        }

    def cleanup_session(
        self, session_id: str, types: list[str] | None = None
    ) -> dict[str, Any]:
        """Delete all artifacts from a specific session."""
        items = self.list(
            session_id=session_id,
            include_expired=True,
            limit=10000,
        )
        if types:
            items = [i for i in items if i.type in types]

        deleted = []
        bytes_freed = 0
        for rec in items:
            deleted.append({"id": rec.id, "type": rec.type})
            bytes_freed += rec.size_bytes
            self.delete(rec.id)

        return {
            "session_id": session_id,
            "deleted_count": len(deleted),
            "bytes_freed": bytes_freed,
            "deleted": deleted,
        }

    # ------------------------------------------------- Session integration

    def record_to_session_ledger(
        self, session_id: str, artifact_id: str
    ) -> None:
        """Append artifact ID to ``agents/<session>/artifacts.jsonl``."""
        session_dir = self._find_session_dir(session_id)
        if session_dir is None:
            return  # no session dir yet — skip silently
        ledger = session_dir / "artifacts.jsonl"
        entry = json.dumps(
            {"artifact_id": artifact_id, "recorded_at": datetime.now(timezone.utc).isoformat()}
        )
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def session_artifacts(self, session_id: str) -> list[str]:
        """Read artifact IDs from a session's ledger."""
        session_dir = self._find_session_dir(session_id)
        if session_dir is None:
            return []
        ledger = session_dir / "artifacts.jsonl"
        if not ledger.exists():
            return []
        ids = []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ids.append(json.loads(line)["artifact_id"])
            except (json.JSONDecodeError, KeyError):
                continue
        return ids

    # ----------------------------------------------------------- Internals

    def _type_dir(self, type: str) -> Path:
        d = self._root / type
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _resolve_ttl(self, type: str, ttl_days: int | None) -> int:
        if ttl_days is not None:
            return ttl_days
        return ARTIFACT_TYPES.get(type, _DEFAULT_TTL_DAYS)

    def _find_meta(self, artifact_id: str) -> Path | None:
        """Locate a ``.meta.json`` by artifact ID across all type dirs."""
        for d in self._root.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            candidate = d / f"{artifact_id}.meta.json"
            if candidate.exists():
                return candidate
        return None

    def _session_dir_name(self, session_id: str | None) -> str | None:
        """Resolve the directory name for a session ID."""
        if not session_id:
            return None
        sd = self._find_session_dir(session_id)
        return sd.name if sd else None

    def _find_session_dir(self, session_id: str) -> Path | None:
        """Find the agents/<timestamp>_<short> dir for a session ID."""
        agents_dir = data_dir("agents")
        if not agents_dir.is_dir():
            return None
        short = session_id[:8]
        for entry in agents_dir.iterdir():
            if entry.is_dir() and entry.name.endswith(f"_{short}"):
                return entry
        return None

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Write via temp file + rename for crash safety."""
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent, prefix=".artifact_", suffix=".tmp"
        )
        try:
            os.write(fd, data)
            os.close(fd)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Module-level convenience (lazy singleton)
# ---------------------------------------------------------------------------

_default_store: ArtifactStore | None = None


def get_store() -> ArtifactStore:
    """Return (or create) the default artifact store."""
    global _default_store
    if _default_store is None:
        _default_store = ArtifactStore()
    return _default_store


def save(
    content: str | bytes,
    type: str,
    slug: str,
    ext: str = "json",
    **kwargs: Any,
) -> ArtifactRecord:
    """Save an artifact.  See :meth:`ArtifactStore.save`."""
    return get_store().save(content, type, slug, ext, **kwargs)


def list_artifacts(**kwargs: Any) -> list[ArtifactRecord]:
    """List artifacts.  See :meth:`ArtifactStore.list`."""
    return get_store().list(**kwargs)


def get(artifact_id: str) -> ArtifactRecord:
    """Get an artifact by ID.  See :meth:`ArtifactStore.get`."""
    return get_store().get(artifact_id)


def read_content(artifact_id: str) -> str:
    """Read artifact content.  See :meth:`ArtifactStore.read_content`."""
    return get_store().read_content(artifact_id)


def delete(artifact_id: str) -> bool:
    """Delete an artifact.  See :meth:`ArtifactStore.delete`."""
    return get_store().delete(artifact_id)


def cleanup(dry_run: bool = True) -> dict[str, Any]:
    """Run TTL-based cleanup.  See :meth:`ArtifactStore.cleanup`."""
    return get_store().cleanup(dry_run=dry_run)


# ---------------------------------------------------------------------------
# Entry-level pruners for singleton resources
# ---------------------------------------------------------------------------


def _run_pruners(dry_run: bool = True) -> list[dict[str, Any]]:
    """Execute all registered pruners from ``paths.PRUNERS``.

    Each pruner is imported lazily and called with the resolved file path
    and its default config.
    """
    import importlib

    from work_buddy.paths import PRUNERS, resolve

    results: list[dict[str, Any]] = []
    for resource_id, (callable_path, default_config) in PRUNERS.items():
        # Resolve path: try RESOURCES registry first, fall back to data_dir
        try:
            path = resolve(resource_id)
        except KeyError:
            # Not a registered singleton — treat as a data_dir category
            # (e.g. "agents/sessions" → data_dir("agents"))
            category = resource_id.split("/")[0]
            path = data_dir(category)
        if not path.exists():
            results.append({"resource": resource_id, "skipped": "path not found"})
            continue

        # Import the pruner function lazily
        module_path, func_name = callable_path.rsplit(".", 1)
        try:
            mod = importlib.import_module(module_path)
            prune_fn = getattr(mod, func_name)
        except (ImportError, AttributeError) as exc:
            results.append({"resource": resource_id, "error": str(exc)})
            continue

        try:
            result = prune_fn(path, default_config, dry_run=dry_run)
            result["resource"] = resource_id
            results.append(result)
        except Exception as exc:
            results.append({"resource": resource_id, "error": str(exc)})

    return results


def prune_chrome_ledger(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Prune chrome tab ledger snapshots older than the rolling window.

    This replaces the inline pruning in ``chrome_ledger.py`` and
    ``chrome_native_host/host.py`` with a centralized implementation
    that runs as part of ``artifact_cleanup``.
    """
    window_days = config.get("window_days", 7)
    cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()

    try:
        raw = path.read_text(encoding="utf-8")
        snapshots = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    # Handle both list format and {"snapshots": [...]} format
    if isinstance(snapshots, dict):
        snapshot_list = snapshots.get("snapshots", [])
    elif isinstance(snapshots, list):
        snapshot_list = snapshots
    else:
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    bytes_before = path.stat().st_size
    kept = [s for s in snapshot_list if s.get("captured_at", "") >= cutoff]
    pruned_count = len(snapshot_list) - len(kept)

    if pruned_count == 0:
        return {
            "pruned": 0,
            "remaining": len(kept),
            "bytes_before": bytes_before,
            "bytes_after": bytes_before,
        }

    if not dry_run:
        # Atomic write
        new_data = json.dumps(kept, ensure_ascii=False)
        temp = path.with_suffix(".tmp")
        temp.write_text(new_data, encoding="utf-8")
        temp.replace(path)
        bytes_after = path.stat().st_size
    else:
        # Estimate new size
        bytes_after = len(json.dumps(kept, ensure_ascii=False).encode("utf-8"))

    return {
        "pruned": pruned_count,
        "remaining": len(kept),
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def prune_llm_cache(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Prune expired entries from the LLM result cache.

    Each entry has an ``expires_at`` ISO timestamp. Entries past expiry
    are removed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        cache = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    if not isinstance(cache, dict):
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    bytes_before = path.stat().st_size
    now = datetime.now()
    kept: dict[str, Any] = {}
    pruned_count = 0

    for task_id, entry in cache.items():
        expires_at = entry.get("expires_at", "")
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) < now:
                    pruned_count += 1
                    continue
            except ValueError:
                pass
        kept[task_id] = entry

    if pruned_count == 0:
        return {
            "pruned": 0,
            "remaining": len(kept),
            "bytes_before": bytes_before,
            "bytes_after": bytes_before,
        }

    if not dry_run:
        new_data = json.dumps(kept, ensure_ascii=False)
        temp = path.with_suffix(".tmp")
        temp.write_text(new_data, encoding="utf-8")
        temp.replace(path)
        bytes_after = path.stat().st_size
    else:
        bytes_after = len(json.dumps(kept, ensure_ascii=False).encode("utf-8"))

    return {
        "pruned": pruned_count,
        "remaining": len(kept),
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def prune_stale_sessions(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Remove agent session directories that haven't been active recently.

    A session is stale if its ``manifest.json`` ``created_at`` is older
    than ``max_age_days`` AND no file in the directory has been modified
    within that window.

    Parameters
    ----------
    path:
        The ``data/agents/`` directory.
    config:
        Must contain ``max_age_days`` (default 14).
    """
    import shutil

    max_age_days = config.get("max_age_days", 14)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    if not path.is_dir():
        return {"pruned": 0, "remaining": 0, "bytes_freed": 0}

    pruned = 0
    remaining = 0
    bytes_freed = 0
    pruned_dirs: list[str] = []

    for entry in path.iterdir():
        if not entry.is_dir():
            continue
        # Skip non-session directories (consent/, operations/, logs/, etc.)
        manifest = entry / "manifest.json"
        if not manifest.exists():
            continue

        # Check creation time from manifest
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            created_str = meta.get("created_at", "")
            created = datetime.fromisoformat(created_str)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (json.JSONDecodeError, ValueError, OSError):
            remaining += 1
            continue

        if created >= cutoff:
            remaining += 1
            continue

        # Check if any file was modified recently (activity = not stale)
        latest_mod = created
        for f in entry.rglob("*"):
            if f.is_file():
                try:
                    mtime = datetime.fromtimestamp(
                        f.stat().st_mtime, tz=timezone.utc
                    )
                    if mtime > latest_mod:
                        latest_mod = mtime
                except OSError:
                    continue

        if latest_mod >= cutoff:
            remaining += 1
            continue

        # Session is stale — calculate size and optionally remove
        dir_size = sum(
            f.stat().st_size for f in entry.rglob("*") if f.is_file()
        )
        pruned_dirs.append(entry.name)
        bytes_freed += dir_size
        pruned += 1

        if not dry_run:
            shutil.rmtree(entry, ignore_errors=True)

    return {
        "pruned": pruned,
        "remaining": remaining,
        "bytes_freed": bytes_freed,
        "pruned_sessions": pruned_dirs,
    }


def prune_old_logs(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Delete log files older than ``max_age_days``.

    Scans ``data/logs/`` for any file and checks its modification time.
    """
    max_age_days = config.get("max_age_days", 7)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    if not path.is_dir():
        return {"pruned": 0, "remaining": 0, "bytes_freed": 0}

    pruned = 0
    remaining = 0
    bytes_freed = 0
    pruned_files: list[str] = []

    for f in path.rglob("*"):
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            remaining += 1
            continue

        if mtime >= cutoff:
            remaining += 1
            continue

        size = f.stat().st_size
        pruned_files.append(f.name)
        bytes_freed += size
        pruned += 1

        if not dry_run:
            f.unlink(missing_ok=True)

    return {
        "pruned": pruned,
        "remaining": remaining,
        "bytes_freed": bytes_freed,
        "pruned_files": pruned_files,
    }
