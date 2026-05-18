"""Filesystem backend — atomic file blobs with ``.meta.json`` sidecars.

Extracted from the original single-file ``work_buddy/artifacts.py``.
The class is the same code that's been shipping; the only change is
that it now also implements the :class:`Storage` protocol so it can
participate in the unified lifecycle system as one backend among
several.

The legacy public name ``ArtifactStore`` remains importable from
:mod:`work_buddy.artifacts` (re-exported in that package's
``__init__.py``) so external callers don't break.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.artifacts.protocol import Capability, Ref
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
    # Transient editing buffers for the docs_checkout / docs_commit
    # materialization workflow. Short TTL — a buffer outlives a single
    # editing session but is swept the next day if a checkout is abandoned.
    "docs_buffer": 1,
}

_DEFAULT_TTL_DAYS = 14  # for unregistered types

_SLUG_RE = re.compile(r"[^a-z0-9_-]")
_TS_FMT = "%Y%m%d-%H%M%S"


# ---------------------------------------------------------------------------
# ArtifactRecord
# ---------------------------------------------------------------------------


@dataclass
class ArtifactRecord:
    """Immutable data container for a single filesystem artifact's identity + metadata.

    Filesystem-specific. The new protocol's :class:`Ref` is the
    cross-backend equivalent.
    """

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
        # Lazy import to avoid circular dep at module load.
        from work_buddy.artifacts.expiry import format_for_user

        d = asdict(self)
        d["path"] = self.path.as_posix()
        d["meta_path"] = self.meta_path.as_posix()
        # Raw ISO for programmatic use.
        d["created_at"] = self.created_at.isoformat()
        d["expires_at"] = self.expires_at.isoformat()
        # User-/agent-facing display strings, formatted in the configured
        # timezone so agents and humans always see a TZ-explicit value
        # rather than raw UTC ISO that can be misinterpreted.
        d["created_at_display"] = format_for_user(self.created_at)
        d["expires_at_display"] = format_for_user(self.expires_at)
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
# FilesystemStorage  (formerly ArtifactStore — same class, new home + protocol)
# ---------------------------------------------------------------------------


class FilesystemStorage:
    """Filesystem backend: atomic file blobs + ``.meta.json`` sidecars.

    Storage protocol capabilities: ``ATOMIC_BLOBS``, ``LISTABLE``,
    ``DELETABLE``.

    Per-type TTL is handled by the lifecycle layer (``PerTypeTtl``
    trigger reading ``ARTIFACT_TYPES``); this class is purely the
    on-disk shape: where files live, how to write them atomically, how
    to enumerate them, how to delete by id.

    Legacy methods (``save``, ``get``, ``list``, ``delete``,
    ``read_content``, ``cleanup``, ``cleanup_session``,
    ``record_to_session_ledger``, ``session_artifacts``) are preserved
    so callers that imported the old ``ArtifactStore`` keep working.
    """

    capabilities: frozenset[Capability] = frozenset({
        Capability.ATOMIC_BLOBS,
        Capability.LISTABLE,
        Capability.DELETABLE,
    })

    def __init__(self, data_root: Path | None = None) -> None:
        if data_root is not None:
            data_root.mkdir(parents=True, exist_ok=True)
            self._root = data_root
        else:
            self._root = data_dir()  # creates the data root if needed

    # ------------------------------------------------------------------ legacy CRUD

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
        atomic_write_bytes(content_path, content_bytes)

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
        atomic_write_bytes(
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
        """Lookup by ID (filename stem). Raises ``FileNotFoundError``."""
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
        """Delete content + meta files. Returns ``True`` if found."""
        meta_path = self._find_meta(artifact_id)
        if meta_path is None:
            return False
        rec = ArtifactRecord.from_meta(meta_path)
        if rec.path.exists():
            rec.path.unlink()
        if rec.meta_path.exists():
            rec.meta_path.unlink()
        return True

    # --------------------------------------------------------------- legacy query

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

    # ----------------------------------------------------------- legacy lifecycle

    def cleanup(self, dry_run: bool = True) -> dict[str, Any]:
        """Unified cleanup driven by the artifact registry.

        Iterates every registered :class:`Artifact` (via ``sweep_all``)
        and aggregates results. The filesystem artifact (registered in
        :mod:`work_buddy.artifacts.default_registrations`) handles what
        was Phase 1; every other registered artifact handles what was
        Phase 2.

        Returns a dict with the same shape the legacy callers expect
        (``artifacts_deleted``, ``artifacts_bytes_freed``, ``deleted``,
        ``pruners``) so external code doesn't break.
        """
        # Lazy import to avoid circular dependency at module load.
        from work_buddy.artifacts.registry import sweep_all

        results = sweep_all(dry_run=dry_run)

        # Project the SweepResult list onto the legacy dict shape.
        # The "filesystem" artifact's result populates the
        # artifacts_deleted/bytes_freed fields; every other result lands
        # in the "pruners" list.
        artifacts_deleted = 0
        artifacts_bytes_freed = 0
        deleted: list[dict[str, Any]] = []
        pruners: list[dict[str, Any]] = []
        for r in results:
            if r.artifact_name == "filesystem":
                artifacts_deleted = r.pruned
                artifacts_bytes_freed = max(0, r.bytes_before - r.bytes_after)
            else:
                pruners.append({
                    "resource": r.artifact_name,
                    "pruned": r.pruned,
                    "remaining": r.remaining,
                    "bytes_before": r.bytes_before,
                    "bytes_after": r.bytes_after,
                    **({"transformed": r.transformed} if r.transformed else {}),
                    **({"error": r.error} if r.error else {}),
                    **r.extra,
                })

        return {
            "dry_run": dry_run,
            "artifacts_deleted": artifacts_deleted,
            "artifacts_bytes_freed": artifacts_bytes_freed,
            "deleted": deleted,
            "pruners": pruners,
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

    # ------------------------------------------------- session integration (legacy)

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

    # ============================================================= Storage protocol

    def iter_records(self) -> Iterable[dict[str, Any]]:
        """Yield each artifact's metadata dict.

        Storage protocol: each record is the raw ``.meta.json`` dict
        plus the resolved meta_path (so the lifecycle can find expired
        ones and ``ref_for`` can construct a Ref).
        """
        if not self._root.exists():
            return
        for d in self._root.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            for meta_file in d.glob("*.meta.json"):
                try:
                    raw = json.loads(meta_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                raw["_meta_path"] = str(meta_file)
                yield raw

    def ref_for(self, record: dict[str, Any]) -> Ref:
        """Storage protocol: extract a Ref from a meta dict."""
        return Ref(
            id=record["id"],
            artifact_name="filesystem",
            metadata={
                "type": record.get("type"),
                "size_bytes": record.get("size_bytes", 0),
                "expires_at": record.get("expires_at"),
                "session_id": record.get("session_id"),
            },
        )

    def delete_record(self, ref: Ref) -> int:
        """Storage protocol: delete by Ref. Returns bytes freed (best-effort)."""
        meta_path = self._find_meta(ref.id)
        if meta_path is None:
            return 0
        try:
            rec = ArtifactRecord.from_meta(meta_path)
        except (json.JSONDecodeError, KeyError, OSError):
            return 0
        bytes_freed = rec.size_bytes
        if rec.path.exists():
            rec.path.unlink()
        if rec.meta_path.exists():
            rec.meta_path.unlink()
        return bytes_freed

    def delete_where(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> tuple[int, int]:
        """Storage protocol: not supported on filesystem (no BULK_PRUNEABLE).

        Per-record deletion is supported via :meth:`delete_record`.
        """
        raise NotImplementedError(
            "FilesystemStorage does not declare BULK_PRUNEABLE; use delete_record"
        )

    def size_bytes(self) -> int:
        """Storage protocol: total bytes used under the root."""
        if not self._root.exists():
            return 0
        total = 0
        for f in self._root.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
        return total

    # ----------------------------------------------------------- internals

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
