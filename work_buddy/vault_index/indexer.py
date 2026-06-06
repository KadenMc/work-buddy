"""Incremental build loop for the vault semantic index.

Reconciles the SQLite chunk store against the current filesystem:

- NEW / CHANGED files (by mtime) are (re)chunked and upserted.
- DELETED files (in the store but no longer discovered) have their chunks pruned.

Mirrors ``work_buddy/ir/store.py::build_index`` and adds the deletion reconciliation
the IR engine lacks. This is the orchestrator that wires ``source`` + ``store`` +
``notifications``; ``store.py`` stays pure persistence.

## Safety invariants (these prevent data loss — do not weaken)

1. **Reconciliation is PER-VAULT, never global.** A global ``set(all_stored) -
   set(all_discovered)`` would prune every *unreachable* vault's items (they're absent
   from the discovered set). The destructive step runs only inside the
   ``reachable=True`` branch, so it is physically unreachable for an offline vault.
2. **``force`` is per-reachable-vault, NEVER a whole-store ``DELETE FROM chunks``.** A
   force run with a vault offline would otherwise delete that vault's chunks, which
   cannot be re-derived (its files are unreachable). An offline vault takes the
   ``if not reachable: warn; continue`` path untouched. Force = "rebuild what I can see."
3. **Reachable-but-empty prunes that vault's chunks — and that's correct.** The
   discriminator is ``VaultStatus.reachable`` (= ``root.is_dir()``): a present-but-empty
   root means the notes were really deleted (converge to empty); a gone root means skip.
4. **A mass-prune is surfaced before it happens** (a reachable vault that had chunks now
   discovers zero files), so it is never silent.

Resumability is by **idempotent per-file commits** (upsert, then mark) — a crash between
them re-treats the file as NEW/CHANGED next run (idempotent via collision-safe doc_id)
and it is never falsely pruned. A single wrapping transaction would be worse (a crash
mid-vault would roll back all progress).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.vault_index import store
from work_buddy.vault_index.source import FilesystemSource

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Warnings (out-of-band, debounced via index_meta)
# ---------------------------------------------------------------------------

def _emit_notification(title: str, body: str, *, tags: list[str]) -> None:
    """Best-effort user-facing notification from this background loop.

    Lazy-imports the notification stack and never raises — a notification
    failure must not abort a build. Patched in tests.
    """
    try:
        from work_buddy.notifications.dispatcher import SurfaceDispatcher
        from work_buddy.notifications.models import (
            Notification,
            NotificationPriority,
            ResponseType,
            SourceType,
        )
        from work_buddy.notifications.store import create_notification

        notif = Notification(
            title=title,
            body=body,
            priority=NotificationPriority.HIGH.value,
            source="vault_index",
            source_type=SourceType.PROGRAMMATIC.value,
            response_type=ResponseType.NONE.value,
            tags=tags,
            expandable=True,
        )
        created = create_notification(notif)
        SurfaceDispatcher.from_config().deliver(created)
    except Exception as exc:  # never let a notification break the build
        logger.warning("vault_index: notification emit failed: %s", exc)


def _warn_unreachable_once(conn, st) -> None:
    """Warn that a vault is unreachable — once per transition into unreachable."""
    key = f"unreachable_warned:{st.vault_id}"
    if store.get_meta(conn, key):
        return  # already warned; debounced until it returns reachable
    _emit_notification(
        title=f"Vault '{st.vault_id}' is unreachable",
        body=(
            f"Root: {st.root}\n"
            f"Reason: {st.reason or 'unknown'}\n\n"
            "Its indexed chunks are KEPT — nothing was deleted. To resolve: "
            "re-point this vault's path in config if it moved, or remove the vault "
            "from config to prune its chunks on the next build."
        ),
        tags=["vault-index", "vault-health", f"vault:{st.vault_id}", "unreachable"],
    )
    store.set_meta(conn, key, _now_iso())


def _clear_unreachable_warning(conn, vault_id: str) -> None:
    """Re-arm the unreachable warning when a vault is reachable again."""
    key = f"unreachable_warned:{vault_id}"
    if store.get_meta(conn, key):
        store.set_meta(conn, key, "")


def _warn_mass_prune(conn, st, count: int) -> None:
    """Surface a full-vault prune (reachable vault, zero files, had chunks)."""
    _emit_notification(
        title=f"Vault '{st.vault_id}': pruning {count} file(s) of chunks",
        body=(
            f"Root: {st.root} is reachable but matched 0 files this build, so "
            f"{count} previously-indexed file(s) will be pruned. If that's "
            "unexpected, check this vault's include/exclude globs in config."
        ),
        tags=["vault-index", "vault-health", f"vault:{st.vault_id}", "mass-prune"],
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_index(cfg: dict | None = None, *, force: bool = False) -> dict:
    """Build/update the vault chunk index, reconciling against the filesystem.

    Args:
        cfg: Config dict (defaults to ``load_config()``).
        force: Rebuild every *reachable* vault from scratch. Never touches an
            unreachable vault (see safety invariant #2).

    Returns:
        Stats dict: vaults, files_new, files_changed, files_deleted,
        files_skipped, chunks_total, vaults_unreachable, build_time_s.
    """
    t0 = time.time()
    if cfg is None:
        cfg = load_config()

    source = FilesystemSource(cfg)
    files, statuses = source.discover()
    conn = store.get_connection(cfg)

    stats = {
        "vaults": 0,
        "files_new": 0,
        "files_changed": 0,
        "files_deleted": 0,
        "files_skipped": 0,
        "chunks_total": 0,
        "vaults_unreachable": 0,
        "build_time_s": 0.0,
    }

    try:
        by_vault: dict[str, list] = {}
        for f in files:
            by_vault.setdefault(f.vault_id, []).append(f)

        for st in statuses:
            # ---- UNREACHABLE: warn (debounced), prune NOTHING (invariant #1/#2) ----
            if not st.reachable:
                stats["vaults_unreachable"] += 1
                _warn_unreachable_once(conn, st)
                continue

            # ---- REACHABLE ----
            stats["vaults"] += 1
            _clear_unreachable_warning(conn, st.vault_id)

            stored = store.get_indexed_items(conn, st.vault_id)  # {item_id: mtime}

            if force:
                # Per-vault rebuild — wipe THIS vault only, then treat all as new.
                for item_id in list(stored):
                    store.delete_item_chunks(conn, item_id)
                stored = {}

            discovered = by_vault.get(st.vault_id, [])
            discovered_ids = {f.item_id for f in discovered}

            # ---- Reconcile deletions (destructive — gated on reachable above) ----
            pruned_ids = set(stored) - discovered_ids
            if stored and not discovered_ids:
                _warn_mass_prune(conn, st, len(pruned_ids))
            for item_id in pruned_ids:
                store.delete_item_chunks(conn, item_id)
                stats["files_deleted"] += 1

            # ---- New / changed / unchanged ----
            for f in discovered:
                prev = stored.get(f.item_id)
                if prev is not None and abs(prev - f.mtime) < 0.001:
                    stats["files_skipped"] += 1
                    continue
                if prev is not None:
                    store.delete_item_chunks(conn, f.item_id)  # drop stale chunks first
                    stats["files_changed"] += 1
                else:
                    stats["files_new"] += 1
                chunks = source.parse(f.item_id)
                store.upsert_chunks(conn, chunks, f.item_id, st.vault_id)   # commit 1
                store.mark_item_indexed(                                    # commit 2
                    conn, f.item_id, mtime=f.mtime, vault_id=st.vault_id,
                    size=f.size, chunk_count=len(chunks),
                )

        stats["chunks_total"] = store.chunk_count(conn)
        store.set_meta(conn, "last_build:vault_index", _now_iso())
        store.set_meta(conn, "chunk_count", str(stats["chunks_total"]))
    finally:
        conn.close()

    stats["build_time_s"] = round(time.time() - t0, 2)
    logger.info("vault_index build: %s", stats)
    return stats


def build_all(cfg: dict | None = None, *, force: bool = False, encode: bool = True) -> dict:
    """Full vault build under the per-index advisory lock.

    Acquires the lock (so the 5-minute rebuild cron's ``is_locked`` check skips a
    run already in progress), builds the SQLite chunk index, then — unless
    ``encode=False`` — encodes new chunks into vectors, heartbeating the lock each
    checkpoint. Returns the index stats with the encode stats under ``"vectors"``.
    """
    if cfg is None:
        cfg = load_config()

    from work_buddy.utils import index_lock
    from work_buddy.vault_index import store

    lock_target = store._db_path(cfg)
    with index_lock.index_lock(lock_target):
        stats = build_index(cfg, force=force)
        if encode:
            from work_buddy.vault_index import dense
            stats["vectors"] = dense.build_vectors(
                cfg, force=force,
                on_checkpoint=lambda: index_lock.refresh(lock_target),
            )
        # If anything changed (files added/changed/deleted or vectors encoded), bump the
        # cache version so search processes reload, and drop the in-process matrix. A
        # no-op tick (all skipped) leaves the version — the resident matrix stays valid.
        changed = bool(
            stats["files_new"] or stats["files_changed"] or stats["files_deleted"]
            or stats.get("vectors", {}).get("vectors_new", 0)
        )
        if changed:
            conn = store.get_connection(cfg)
            try:
                prev = int(store.get_meta(conn, "build_version:vault") or "0")
                store.set_meta(conn, "build_version:vault", str(prev + 1))
            finally:
                conn.close()
            from work_buddy.vault_index import dense_cache
            dense_cache.invalidate()
    return stats
