"""Machine-level registry for targeted truth stores.

The registry is an inventory and health index. It is not a second source of
truth for store metadata. Every reachable row is validated against the
store's ``store.yaml`` and ``store_info`` row before it is returned.
"""

from __future__ import annotations

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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_sidecar(path_or_root: str | Path) -> Path:
    sidecar = StorePaths.from_root(path_or_root).sidecar.resolve()
    return Path(os.path.normcase(str(sidecar)))


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
        return RegisteredTruthStore(
            path=Path(row["path"]),
            store_id=row["store_id"],
            profile=row["profile"],
            title=row["title"],
            last_seen=row["last_seen"],
            reachable=bool(row["reachable"]),
        )

    @staticmethod
    def _observe(path: Path) -> TruthStore:
        return TruthStore.open(path)

    def _rows_for_store_id(self, store_id: str) -> list[RegisteredTruthStore]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path, store_id, profile, title, last_seen, reachable "
                "FROM truth_stores WHERE store_id = ? ORDER BY path",
                (store_id,),
            ).fetchall()
            return [self._record(row) for row in rows]
        finally:
            conn.close()

    def _set_unreachable(self, path: Path) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE truth_stores SET reachable = 0 WHERE path = ?",
                (str(path),),
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
        profile = store.profile
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT store_id FROM truth_stores WHERE path = ?",
                (str(path),),
            ).fetchone()
            if existing is not None and existing["store_id"] != store.store_id:
                raise RegistryIdentityMismatch(
                    f"registered path {path} changed identity from "
                    f"{existing['store_id']} to {store.store_id}"
                )
            conn.execute(
                """
                INSERT INTO truth_stores (
                    path, store_id, profile, title, last_seen, reachable
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    profile = excluded.profile,
                    title = excluded.title,
                    last_seen = excluded.last_seen,
                    reachable = excluded.reachable
                """,
                (
                    str(path),
                    store.store_id,
                    profile.profile,
                    profile.title,
                    observed_at,
                    int(reachable),
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
            if row.path == path:
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
            self._set_unreachable(row.path)
            return RegisteredTruthStore(
                path=row.path,
                store_id=row.store_id,
                profile=row.profile,
                title=row.title,
                last_seen=row.last_seen,
                reachable=False,
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
                    "SELECT path, store_id, profile, title, last_seen, reachable "
                    "FROM truth_stores ORDER BY path"
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
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT path, store_id, profile, title, last_seen, reachable "
                "FROM truth_stores WHERE path = ?",
                (str(path),),
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

    def open_store(self, store_id: str) -> TruthStore:
        """Open and touch the single reachable store for an identity."""
        row = self.get_by_store_id(store_id, refresh=True)
        if row is None:
            raise TruthRegistryError(f"truth store is not reachable: {store_id}")
        store = self._observe(row.path)
        self.touch(store)
        return store

    def unregister(self, path_or_root: str | Path) -> bool:
        """Remove one historical path from the machine registry."""
        path = _canonical_sidecar(path_or_root)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM truth_stores WHERE path = ?",
                (str(path),),
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
