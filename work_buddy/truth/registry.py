"""Machine-level registry for targeted truth stores.

The registry is an inventory and health index. It is not a second source of
truth for store metadata. Every reachable row is validated against the
store's ``store.yaml`` and ``store_info`` row before it is returned.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from work_buddy.truth.contracts import StorePaths
from work_buddy.truth.export import StoreIdentityCollision
from work_buddy.truth.registry_migrations import TRUTH_REGISTRY_MIGRATIONS
from work_buddy.truth.store import TruthStore


class TruthRegistryError(RuntimeError):
    """Base error for machine-level truth registry operations."""


class RegistryIdentityMismatch(TruthRegistryError):
    """A registered path now carries a different truth store identity."""


@dataclass(frozen=True, slots=True)
class RegisteredTruthStore:
    """The frozen public row returned by the truth store registry."""

    path: Path
    store_id: str
    profile: str
    title: str | None
    last_seen: str
    reachable: bool
    layout: str
    document_surface_enabled: bool
    allowed_document_classes: tuple[str, ...]
    feedback_capture: bool
    document_count: int
    last_error: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _filesystem_display_path(path: Path) -> Path:
    """Recover on-disk component casing without changing path identity.

    Windows comparisons are case-insensitive, but Folder basename/full-path
    copy is user-facing. ``normcase`` therefore belongs only in ``path_key``;
    persisting it as the open/display path would turn `Reference Folder` into
    `reference folder`. Directory enumeration recovers existing component
    names for old registry rows that were already normalized.
    """

    resolved = path.expanduser().resolve()
    if os.name != "nt" or not resolved.exists():
        return resolved
    parts = resolved.parts
    if not parts:
        return resolved
    current = Path(parts[0])
    for part in parts[1:]:
        try:
            match = next(
                (
                    entry.name
                    for entry in current.iterdir()
                    if entry.name.casefold() == part.casefold()
                ),
                part,
            )
        except OSError:
            match = part
        current /= match
    return current


def _canonical_sidecar(path_or_root: str | Path) -> Path:
    sidecar = StorePaths.from_root(path_or_root).sidecar.resolve()
    return _filesystem_display_path(sidecar)


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _layout_for_sidecar(path: Path) -> str:
    return "wbuddy_cowork_v1"


_SELECT_COLUMNS = (
    "path, store_id, profile, title, last_seen, reachable, layout, "
    "document_surface_enabled, allowed_document_classes_json, "
    "feedback_capture, document_count, last_error"
)


class TruthStoreRegistry:
    """SQLite registry of known truth stores and their current health."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        if db_path is None:
            from work_buddy.paths import resolve

            db_path = resolve("db/truth-registry")
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        conn = self._connect()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            TRUTH_REGISTRY_MIGRATIONS.run(conn)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _record(row: sqlite3.Row) -> RegisteredTruthStore:
        try:
            allowed = json.loads(row["allowed_document_classes_json"])
        except (TypeError, ValueError):
            allowed = []
        if not isinstance(allowed, list):
            allowed = []
        return RegisteredTruthStore(
            path=Path(row["path"]),
            store_id=row["store_id"],
            profile=row["profile"],
            title=row["title"],
            last_seen=row["last_seen"],
            reachable=bool(row["reachable"]),
            layout=row["layout"],
            document_surface_enabled=bool(row["document_surface_enabled"]),
            allowed_document_classes=tuple(str(item) for item in allowed),
            feedback_capture=bool(row["feedback_capture"]),
            document_count=int(row["document_count"]),
            last_error=row["last_error"],
        )

    @staticmethod
    def _observe(path: Path) -> TruthStore:
        return TruthStore.open(path)

    def _rows_for_store_id(self, store_id: str) -> list[RegisteredTruthStore]:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM truth_stores "
                "WHERE store_id = ? ORDER BY path",
                (store_id,),
            ).fetchall()
            return [self._record(row) for row in rows]
        finally:
            conn.close()

    def _set_unreachable(self, path: Path, error: str | None = None) -> None:
        path_key = _path_key(path)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE truth_stores SET reachable = 0, last_error = ? "
                "WHERE path_key = ?",
                (error, path_key),
            )
            conn.commit()
        finally:
            conn.close()

    def _record_observation(
        self,
        path: Path,
        store: TruthStore,
        *,
        reachable: bool,
        observed_at: str,
    ) -> RegisteredTruthStore:
        path = _canonical_sidecar(path)
        path_key = _path_key(path)
        profile = store.profile
        policy = profile.document_surface
        with store._read_connection() as read_conn:
            document_count = int(
                read_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            )
        layout = _layout_for_sidecar(path)
        allowed_json = json.dumps(
            list(policy.allowed_document_classes), separators=(",", ":")
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT path, store_id FROM truth_stores WHERE path_key = ?",
                (path_key,),
            ).fetchone()
            if existing is not None and existing["store_id"] != store.store_id:
                raise RegistryIdentityMismatch(
                    f"registered path {path} changed identity from "
                    f"{existing['store_id']} to {store.store_id}"
                )
            conn.execute(
                """
                INSERT INTO truth_stores (
                    path, path_key, store_id, profile, title, last_seen, reachable,
                    layout, document_surface_enabled,
                    allowed_document_classes_json, feedback_capture,
                    document_count, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(path_key) DO UPDATE SET
                    path = excluded.path,
                    profile = excluded.profile,
                    title = excluded.title,
                    last_seen = excluded.last_seen,
                    reachable = excluded.reachable,
                    layout = excluded.layout,
                    document_surface_enabled = excluded.document_surface_enabled,
                    allowed_document_classes_json = excluded.allowed_document_classes_json,
                    feedback_capture = excluded.feedback_capture,
                    document_count = excluded.document_count,
                    last_error = NULL
                """,
                (
                    str(path),
                    path_key,
                    store.store_id,
                    profile.profile,
                    profile.title,
                    observed_at,
                    int(reachable),
                    layout,
                    int(policy.enabled),
                    allowed_json,
                    int(policy.feedback_capture),
                    document_count,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StoreIdentityCollision(
                f"store_id {store.store_id} is already reachable at another path"
            ) from exc
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        return RegisteredTruthStore(
            path=path,
            store_id=store.store_id,
            profile=profile.profile,
            title=profile.title,
            last_seen=observed_at,
            reachable=reachable,
            layout=layout,
            document_surface_enabled=policy.enabled,
            allowed_document_classes=tuple(policy.allowed_document_classes),
            feedback_capture=policy.feedback_capture,
            document_count=document_count,
            last_error=None,
        )

    def register(
        self,
        path_or_store: str | Path | TruthStore,
    ) -> RegisteredTruthStore:
        """Validate and register a store, refusing another live identity."""
        path = _canonical_sidecar(
            path_or_store.paths.sidecar
            if isinstance(path_or_store, TruthStore)
            else path_or_store
        )
        store = self._observe(path)
        now = self._clock()

        live_elsewhere: list[Path] = []
        for row in self._rows_for_store_id(store.store_id):
            if _path_key(row.path) == _path_key(path):
                continue
            try:
                other = self._observe(row.path)
            except Exception:
                self._set_unreachable(row.path)
                continue
            if other.store_id == store.store_id:
                live_elsewhere.append(row.path)
            else:
                self._set_unreachable(row.path)
        if live_elsewhere:
            locations = ", ".join(str(item) for item in live_elsewhere)
            raise StoreIdentityCollision(
                f"store_id {store.store_id} is already reachable at {locations}"
            )
        try:
            return self._record_observation(
                path,
                store,
                reachable=True,
                observed_at=now,
            )
        except RegistryIdentityMismatch:
            self._set_unreachable(path)
            raise

    def touch(
        self,
        path_or_store: str | Path | TruthStore,
    ) -> RegisteredTruthStore:
        """Revalidate one accessed store and refresh its last-seen time."""
        return self.register(path_or_store)

    def _refresh_path(
        self,
        row: RegisteredTruthStore,
        *,
        raise_collision: bool,
    ) -> RegisteredTruthStore:
        try:
            observed = self._observe(row.path)
        except Exception:
            self._set_unreachable(row.path, "store_unreachable")
            return RegisteredTruthStore(
                path=row.path,
                store_id=row.store_id,
                profile=row.profile,
                title=row.title,
                last_seen=row.last_seen,
                reachable=False,
                layout=row.layout,
                document_surface_enabled=row.document_surface_enabled,
                allowed_document_classes=row.allowed_document_classes,
                feedback_capture=row.feedback_capture,
                document_count=row.document_count,
                last_error="store_unreachable",
            )
        if observed.store_id != row.store_id:
            self._set_unreachable(row.path)
            raise RegistryIdentityMismatch(
                f"registered path {row.path} carries store_id "
                f"{observed.store_id}, expected {row.store_id}"
            )
        try:
            return self._record_observation(
                row.path,
                observed,
                reachable=True,
                observed_at=self._clock(),
            )
        except StoreIdentityCollision:
            unavailable = self._record_observation(
                row.path,
                observed,
                reachable=False,
                observed_at=self._clock(),
            )
            if raise_collision:
                raise
            return unavailable

    def list_stores(self, *, refresh: bool = True) -> tuple[RegisteredTruthStore, ...]:
        """List registered stores in stable path order."""
        if refresh:
            conn = self._connect()
            try:
                store_ids = [
                    row["store_id"]
                    for row in conn.execute(
                        "SELECT DISTINCT store_id FROM truth_stores ORDER BY store_id"
                    ).fetchall()
                ]
            finally:
                conn.close()
            for store_id in store_ids:
                try:
                    self.paths_for_store_id(store_id)
                except StoreIdentityCollision:
                    # ``paths_for_store_id`` marks every physically reachable
                    # duplicate unavailable before raising. Listing remains a
                    # health-reporting surface, so retain those fail-closed
                    # rows instead of selecting an arbitrary live copy.
                    pass

        conn = self._connect()
        try:
            rows = [
                self._record(row)
                for row in conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM truth_stores ORDER BY path"
                ).fetchall()
            ]
        finally:
            conn.close()
        return tuple(rows)

    def get_by_path(
        self,
        path_or_root: str | Path,
        *,
        refresh: bool = True,
    ) -> RegisteredTruthStore | None:
        """Return one registered path, optionally revalidating it first."""
        path = _canonical_sidecar(path_or_root)
        path_key = _path_key(path)
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM truth_stores WHERE path_key = ?",
                (path_key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        record = self._record(row)
        return self._refresh_path(record, raise_collision=True) if refresh else record

    def paths_for_store_id(self, store_id: str) -> tuple[Path, ...]:
        """Return the single reachable path for an identity, or no paths."""
        rows = self._rows_for_store_id(store_id)
        observed: list[tuple[RegisteredTruthStore, TruthStore, str]] = []
        for row in rows:
            try:
                store = self._observe(row.path)
            except Exception:
                self._set_unreachable(row.path)
                continue
            if store.store_id != store_id:
                self._set_unreachable(row.path)
                continue
            observed.append((row, store, self._clock()))

        if len(observed) > 1:
            for row, store, seen in observed:
                self._record_observation(
                    row.path,
                    store,
                    reachable=False,
                    observed_at=seen,
                )
            locations = ", ".join(str(row.path) for row, _, _ in observed)
            raise StoreIdentityCollision(
                f"store_id {store_id} is reachable at multiple paths: {locations}"
            )
        if not observed:
            return ()
        row, store, seen = observed[0]
        current = self._record_observation(
            row.path,
            store,
            reachable=True,
            observed_at=seen,
        )
        return (current.path,)

    def get_by_store_id(
        self,
        store_id: str,
        *,
        refresh: bool = True,
    ) -> RegisteredTruthStore | None:
        """Return the live row for a store identity."""
        if refresh:
            paths = self.paths_for_store_id(store_id)
            if not paths:
                return None
            return self.get_by_path(paths[0], refresh=False)
        rows = [row for row in self._rows_for_store_id(store_id) if row.reachable]
        if len(rows) > 1:
            raise StoreIdentityCollision(
                f"store_id {store_id} has multiple reachable registry rows"
            )
        return rows[0] if rows else None

    def relocate(
        self,
        old_path_or_root: str | Path,
        new_path_or_root: str | Path,
        *,
        store_id: str,
    ) -> RegisteredTruthStore:
        """Atomically move one registry row after a validated filesystem move.

        The new sidecar must already be readable and carry ``store_id``.  This
        avoids the unregister/register gap that could lose inventory or admit a
        duplicate identity when a canonical Folder is moved.
        """

        old_path = _canonical_sidecar(old_path_or_root)
        new_path = _canonical_sidecar(new_path_or_root)
        old_path_key = _path_key(old_path)
        new_path_key = _path_key(new_path)
        observed = self._observe(new_path)
        if observed.store_id != store_id:
            raise RegistryIdentityMismatch(
                f"relocation target {new_path} carries store_id "
                f"{observed.store_id}, expected {store_id}"
            )
        profile = observed.profile
        policy = profile.document_surface
        with observed._read_connection() as read_conn:
            document_count = int(
                read_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            )
        now = self._clock()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT store_id FROM truth_stores WHERE path_key = ?",
                (old_path_key,),
            ).fetchone()
            target = conn.execute(
                "SELECT store_id FROM truth_stores WHERE path_key = ?",
                (new_path_key,),
            ).fetchone()
            if source is None:
                raise TruthRegistryError(
                    f"registry relocation source does not exist: {old_path}"
                )
            if source["store_id"] != store_id:
                raise RegistryIdentityMismatch(
                    f"registry relocation source carries {source['store_id']}, "
                    f"expected {store_id}"
                )
            if target is not None and new_path_key != old_path_key:
                raise TruthRegistryError(
                    f"registry relocation target already exists: {new_path}"
                )
            conn.execute(
                """
                UPDATE truth_stores SET
                    path = ?, path_key = ?, profile = ?, title = ?, last_seen = ?,
                    reachable = 1, layout = ?,
                    document_surface_enabled = ?,
                    allowed_document_classes_json = ?, feedback_capture = ?,
                    document_count = ?, last_error = NULL
                WHERE path_key = ? AND store_id = ?
                """,
                (
                    str(new_path),
                    new_path_key,
                    profile.profile,
                    profile.title,
                    now,
                    _layout_for_sidecar(new_path),
                    int(policy.enabled),
                    json.dumps(
                        list(policy.allowed_document_classes), separators=(",", ":")
                    ),
                    int(policy.feedback_capture),
                    document_count,
                    old_path_key,
                    store_id,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StoreIdentityCollision(
                f"store_id {store_id} is already registered at another path"
            ) from exc
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        row = self.get_by_path(new_path, refresh=False)
        if row is None:  # pragma: no cover - guarded by the transaction above
            raise TruthRegistryError("registry relocation did not publish its target")
        return row

    def register_projection(
        self,
        path_or_root: str | Path,
        *,
        store_id: str,
        profile: str,
        title: str | None,
        document_surface_enabled: bool,
        allowed_document_classes: tuple[str, ...] | list[str],
        feedback_capture: bool,
        document_count: int,
    ) -> RegisteredTruthStore:
        """Register a separately validated read-only store observation.

        Folder adoption uses this seam after immutable profile/SQLite checks so
        adding machine inventory cannot run store migrations, change PRAGMAs,
        or create WAL/SHM files inside the selected Folder.
        """

        path = _canonical_sidecar(path_or_root)
        path_key = _path_key(path)
        now = self._clock()
        allowed_json = json.dumps(
            list(allowed_document_classes), separators=(",", ":")
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT path, store_id FROM truth_stores WHERE path_key = ?",
                (path_key,),
            ).fetchone()
            if existing is not None and existing["store_id"] != store_id:
                raise RegistryIdentityMismatch(
                    f"registered path {path} changed identity from "
                    f"{existing['store_id']} to {store_id}"
                )
            conn.execute(
                """
                INSERT INTO truth_stores (
                    path, path_key, store_id, profile, title, last_seen, reachable,
                    layout, document_surface_enabled,
                    allowed_document_classes_json, feedback_capture,
                    document_count, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(path_key) DO UPDATE SET
                    path = excluded.path,
                    profile = excluded.profile,
                    title = excluded.title,
                    last_seen = excluded.last_seen,
                    reachable = 1,
                    layout = excluded.layout,
                    document_surface_enabled = excluded.document_surface_enabled,
                    allowed_document_classes_json = excluded.allowed_document_classes_json,
                    feedback_capture = excluded.feedback_capture,
                    document_count = excluded.document_count,
                    last_error = NULL
                """,
                (
                    str(path),
                    path_key,
                    store_id,
                    profile,
                    title,
                    now,
                    _layout_for_sidecar(path),
                    int(document_surface_enabled),
                    allowed_json,
                    int(feedback_capture),
                    int(document_count),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise StoreIdentityCollision(
                f"store_id {store_id} is already reachable at another path"
            ) from exc
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        row = self.get_by_path(path, refresh=False)
        if row is None:  # pragma: no cover - guarded by the upsert above
            raise TruthRegistryError("validated store projection was not registered")
        return row

    def open_store(self, store_id: str) -> TruthStore:
        """Open and touch the single reachable canonical store."""

        row = self.get_by_store_id(store_id, refresh=True)
        if row is None:
            raise TruthRegistryError(f"truth store is not reachable: {store_id}")
        store = self._observe(row.path)
        # Opening is the common restart/request seam. Recovery takes each
        # operation's normal path/document lock and re-reads state after it, so
        # a live sibling publisher is never relabelled as abandoned.
        from work_buddy.cowork.recovery import recover_store_persistence

        recover_store_persistence(store)
        self.touch(store)
        return store

    def unregister(self, path_or_root: str | Path) -> bool:
        """Remove one historical path from the machine registry."""
        path = _canonical_sidecar(path_or_root)
        path_key = _path_key(path)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM truth_stores WHERE path_key = ?",
                (path_key,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


__all__ = [
    "RegisteredTruthStore",
    "RegistryIdentityMismatch",
    "TruthRegistryError",
    "TruthStoreRegistry",
]
