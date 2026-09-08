"""IndexBuilder — incremental, resumable, locked build of one partition.

Flow (under the DB-wide writer gate + the per-partition advisory lock):
  discover → diff by ``change_key`` (content-hash default, or mtime) → for each
  changed item: delete its old docs, parse, upsert, encode projections (BACKGROUND,
  batched across docs; optionally deferred to the global resume pass) → prune deleted
  items → if anything changed, bump the partition ``build_version`` and invalidate its
  resident matrices.

Two advisory locks, both heartbeated for the whole hold:
- ``<db>.build`` — the DB-wide WRITER GATE. SQLite allows one writer per DB and all
  partitions share one DB, so builds serialize across partitions AND processes
  (sidecar refresh jobs, the embedding service's build endpoint, the CLI). The
  refresh jobs probe this gate read-only and self-skip while any build runs.
- ``<db>.<partition>`` — the per-partition identity lock (status probes, and the
  same-partition self-skip message).

Generalizes ``vault_index/indexer.py`` + ``ir/store.build_index`` and adds the advisory
lock the IR build lacked. Resumable: re-encodes any docs still missing vectors, in
bounded batches so each pass commits durable progress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from work_buddy.index.partition import (
    get_change_key,
    get_projection_schema,
)
from work_buddy.logging_config import get_logger

if TYPE_CHECKING:
    from work_buddy.index.encode import Encoder
    from work_buddy.index.partition import Partition
    from work_buddy.index.resident import ResidentCacheRegistry
    from work_buddy.index.store import IndexStore

logger = get_logger(__name__)


class IndexBuilder:
    def __init__(
        self,
        store: "IndexStore",
        encoder: "Encoder",
        partition: "Partition",
        *,
        cfg: Any = None,
        residents: "ResidentCacheRegistry | None" = None,
        use_lock: bool = True,
    ) -> None:
        self._store = store
        self._encoder = encoder
        self._partition = partition
        self._cfg = cfg  # PartitionConfig; None → retention defaults to track_source
        self._use_lock = use_lock
        if residents is None:
            from work_buddy.index.resident import get_registry
            residents = get_registry()
        self._residents = residents

    def _lock_ctx(self):
        from work_buddy.index.locking import index_writer_locks

        return index_writer_locks(
            self._store.db_path,
            self._partition.name,
            enabled=self._use_lock,
        )

    def build(
        self, *, force: bool = False,
        on_progress: Callable[[dict], None] | None = None,
        after_build: Callable[[dict[str, Any]], None] | None = None,
        defer_changed_vectors: bool = False,
        max_items: int | None = None,
        max_vector_batches: int | None = None,
    ) -> dict[str, Any]:
        """Reconcile the partition and finish any missing-vector backfill.

        ``defer_changed_vectors`` is an opt-in for bounded maintenance builds with
        many changed source items.  It keeps each item's lexical documents and
        change-ledger entry durable as usual, but leaves vector work to the global,
        resumable ``_encode_missing`` pass below.  Ordinary builds retain immediate
        per-item encoding by default.

        ``max_items`` caps changed/deleted source items reconciled in this invocation;
        ``max_vector_batches`` caps global missing-vector batches. These deterministic
        budgets turn large scheduled backlogs into resumable slices. A partial slice
        deliberately does not stamp ``last_build`` or invoke ``after_build`` (which is
        the outbox acknowledgement boundary). ``force`` cannot be combined with either
        budget because every pass would reselect source items and invalidate their vectors
        without a persisted force-phase cursor.

        A source item is the atomic lexical unit: the budget does not split one parsed
        item's documents or avoid full source discovery. It bounds ordinary backlog
        catch-up, while an exceptionally large individual item can still take longer.
        """
        if max_items is not None and (
            isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0
        ):
            raise ValueError("max_items must be a positive integer")
        if max_vector_batches is not None and (
            isinstance(max_vector_batches, bool)
            or not isinstance(max_vector_batches, int)
            or max_vector_batches <= 0
        ):
            raise ValueError("max_vector_batches must be a positive integer")
        if force and (max_items is not None or max_vector_batches is not None):
            raise ValueError("work budgets cannot be combined with force=True")
        if max_items is not None or max_vector_batches is not None:
            defer_changed_vectors = True
        # Schema creation/repair is an explicit operation with its own long-lived
        # acquisition of the same DB-wide writer gate. Ordinary reads, including
        # dashboard status, never reach this forward-only boundary.
        self._store.prepare_schema(repair_existing=False)
        pname = self._partition.name
        change_key = get_change_key(self._partition)
        schema = get_projection_schema(self._partition)

        with self._lock_ctx():
            mutation_started = self._store.partition_mutation_in_progress(pname)
            if mutation_started:
                # A prior interrupted writer left the durable fence in place. Keep
                # dense readers out until this reconciliation successfully finalizes.
                for projection_name in schema:
                    self._residents.invalidate(f"{pname}:{projection_name}")

            def _begin_mutation() -> None:
                """Fence resident readers before the first durable mutation.

                Vector/document writes commit in their own short transactions. The dirty
                marker makes version reads unavailable throughout that multi-transaction
                window; successful finalization atomically bumps the generation and clears
                the marker. A crash leaves the fence durable for the next replay.
                """
                nonlocal mutation_started
                if mutation_started:
                    return
                self._store.begin_partition_mutation(pname)
                for projection_name in schema:
                    self._residents.invalidate(f"{pname}:{projection_name}")
                mutation_started = True

            indexed = self._store.get_indexed_items(pname)  # {item_id: (mtime, hash)}
            discovered = list(self._partition.discover())
            disc_ids = {ref.item_id for ref in discovered}

            # --- diff ---
            changed = []
            for ref in discovered:
                prev = indexed.get(ref.item_id)
                if force or prev is None:
                    changed.append(ref)
                elif change_key == "mtime":
                    if abs(prev[0] - ref.mtime) > 1e-6:
                        changed.append(ref)
                else:  # hash
                    if (ref.content_hash or "") != (prev[1] or ""):
                        changed.append(ref)

            deleted = [iid for iid in indexed if iid not in disc_ids]
            # A cursorless bounded first build must make forward progress even when
            # newly indexed head items change again before the next tick. Preserve
            # discovery order within each class, but always drain never-indexed work
            # before refreshing entries that already have a durable ledger row.
            changed.sort(key=lambda ref: ref.item_id in indexed)
            changed_total = len(changed)
            deleted_total = len(deleted)
            if max_items is not None:
                # Remove stale source items first, then spend the remainder on changed
                # items. Both operations are ledger-backed, so the unprocessed tail is
                # rediscovered naturally on the next run without a second cursor store.
                deleted = deleted[:max_items]
                remaining_slots = max_items - len(deleted)
                changed = changed[:remaining_slots]
            if changed or deleted:
                _begin_mutation()
            self._prune(pname, deleted, before_write=_begin_mutation)
            remaining_items = (changed_total - len(changed)) + (deleted_total - len(deleted))

            # --- parse + upsert + encode changed items ---
            n_docs = 0
            for i, ref in enumerate(changed):
                self._store.delete_item_docs(ref.item_id, partition=pname)  # clear stale
                docs = self._partition.parse(ref.item_id)
                for d in docs:
                    if not d.partition:
                        d.partition = pname
                    d.ensure_hash()
                self._store.upsert_documents(docs, item_id=ref.item_id)
                if not defer_changed_vectors:
                    self._encode_docs(docs, schema)
                self._store.mark_item_indexed(
                    ref.item_id, pname, mtime=ref.mtime,
                    content_hash=ref.content_hash or "", doc_count=len(docs),
                )
                n_docs += len(docs)
                if on_progress:
                    on_progress({"phase": "indexing", "done": i + 1, "total": len(changed)})

            # --- resume: encode any docs still missing a vector ---
            vector_batches = 0
            vectors_encoded = 0
            remaining_vectors: dict[str, int] = {}
            for proj_name, spec in schema.items():
                batch_budget = (
                    None
                    if max_vector_batches is None
                    else max(0, max_vector_batches - vector_batches)
                )
                encoded, batches, remaining = self._encode_missing(
                    pname,
                    proj_name,
                    spec,
                    max_batches=batch_budget,
                    before_write=_begin_mutation,
                )
                vectors_encoded += encoded
                vector_batches += batches
                remaining_vectors[proj_name] = remaining

            if mutation_started:
                self._store.finish_partition_mutation(pname)
                for projection_name in schema:
                    self._residents.invalidate(f"{pname}:{projection_name}")

            index_complete = remaining_items == 0 and not any(remaining_vectors.values())
            stats = {
                "partition": pname,
                "changed": len(changed),
                "changed_total": changed_total,
                "deleted": len(deleted),
                "deleted_total": deleted_total,
                "docs_indexed": n_docs,
                "vectors_encoded": vectors_encoded,
                "vector_batches": vector_batches,
                "remaining": {
                    "items": remaining_items,
                    "vectors": remaining_vectors,
                },
                "index_complete": index_complete,
                # A plain completion hook has no delivery phase, so it should
                # observe a completed build immediately.  Domain adapters with
                # a real delivery boundary (for example the database outbox)
                # mark these provisional values false before reconciling it.
                "delivery_complete": index_complete,
                "complete": index_complete,
                "doc_count": self._store.doc_count(pname),
                "version": self._store.build_version(pname),
            }
            # Domain delivery reconciliation belongs to the same writer-gate
            # hold. The index commit is already durable, but no second builder
            # can interleave between its parity check and exact outbox ack.
            if index_complete and after_build is not None:
                after_build(stats)
            delivery_complete = index_complete and (
                not isinstance(stats.get("outbox"), dict)
                or bool(stats["outbox"].get("ready"))
            )
            complete = index_complete and delivery_complete
            stats["delivery_complete"] = delivery_complete
            stats["complete"] = complete

            # A successful empty/no-op build is still material cutover evidence:
            # it proves the registered native source and any durable outbox boundary
            # were reconciled, including a legitimately empty corpus. A bounded,
            # inference-deferred, parity-raced, or newly-appended-outbox run must not
            # masquerade as an end-to-end completed build.
            if complete:
                from datetime import datetime, timezone
                self._store.set_meta(
                    f"last_build:{pname}", datetime.now(timezone.utc).isoformat()
                )
            logger.info("index build [%s]: %s", pname, stats)
            return stats

    # -- retention --------------------------------------------------------
    def _prune(
        self,
        pname: str,
        deleted: list[str],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """Apply the partition's RETENTION policy to items the source has dropped.

        - ``track_source`` (default): delete them — the index mirrors the live source.
        - ``retain``: keep them as orphans (``mark_items_orphaned`` stamps + forgets the
          ledger), so search recall survives source deletion.
        - ``ttl``: keep newly-gone items as orphans too, then sweep orphans older than
          ``retention_ttl_days`` to bound growth.
        """
        if not deleted:
            # Still run the TTL sweep so orphans age out even on a no-new-deletions tick.
            if getattr(self._cfg, "retention", "track_source") == "ttl":
                self._sweep_orphans(pname, before_write=before_write)
            return
        retention = getattr(self._cfg, "retention", "track_source") if self._cfg else "track_source"
        if retention == "track_source":
            for iid in deleted:
                self._store.delete_item_docs(iid, partition=pname)
            return
        # retain / ttl: newly-gone items become orphans (kept, ledger forgotten).
        self._store.mark_items_orphaned(deleted, pname)
        if retention == "ttl":
            self._sweep_orphans(pname, before_write=before_write)

    def _sweep_orphans(
        self,
        pname: str,
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """TTL mode: delete orphans whose newest doc timestamp is older than the window."""
        ttl_days = getattr(self._cfg, "retention_ttl_days", None) or 0
        if ttl_days and ttl_days > 0:
            import time
            cutoff = time.time() - ttl_days * 86400.0
            if self._store.has_orphans_older_than(pname, cutoff):
                if before_write is not None:
                    before_write()
                self._store.prune_orphans_older_than(pname, cutoff)

    # -- encoding helpers -------------------------------------------------
    def _encode_docs(self, docs: list, schema: dict) -> None:
        """Encode each projection across the given docs in ONE batched call/projection.

        Handles pooled (list) projections by flattening then regrouping per doc.
        """
        for proj_name, spec in schema.items():
            flat_texts: list[str] = []
            layout: list[tuple[str, int, int]] = []  # (doc_id, start, count)
            for d in docs:
                p = d.projections.get(proj_name)
                if p is None:
                    continue
                text = p.text
                if isinstance(text, list):
                    texts = [t for t in text if t]
                    if not texts:
                        continue
                    layout.append((d.doc_id, len(flat_texts), len(texts)))
                    flat_texts.extend(texts)
                elif text:
                    layout.append((d.doc_id, len(flat_texts), 1))
                    flat_texts.append(text)
            if not flat_texts:
                continue
            vecs = self._encoder.encode_documents(flat_texts, spec.kind, model_key=spec.model_key)
            if vecs is None:
                logger.warning(
                    "encode unavailable for %s/%s; lexical indexed, dense deferred",
                    self._partition.name, proj_name,
                )
                continue
            rows = [(doc_id, vecs[start:start + count]) for doc_id, start, count in layout]
            self._store.upsert_vectors(proj_name, rows)

    # Docs per encode→write cycle in the vector backfill. Bounds both the write
    # transaction (a short writer hold, not one giant multi-thousand-row commit)
    # and the unit of loss: each batch is durable the moment it lands, so an
    # interrupted backfill resumes from the last batch, not from zero.
    _ENCODE_MISSING_BATCH = 256

    def _encode_missing(
        self,
        pname: str,
        projection: str,
        spec,
        *,
        max_batches: int | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> tuple[int, int, int]:
        """Encode a bounded slice, returning ``(docs, batches, remaining_docs)``."""
        doc_limit = (
            None
            if max_batches is None
            else max_batches * self._ENCODE_MISSING_BATCH
        )
        if doc_limit == 0:
            return 0, 0, self._store.missing_vector_count(pname, projection)
        work = self._store.docs_missing_vectors(
            pname,
            projection,
            limit=doc_limit,
        )
        encoded = 0
        batches = 0
        for batch_start in range(0, len(work), self._ENCODE_MISSING_BATCH):
            if max_batches is not None and batches >= max_batches:
                break
            batch = work[batch_start:batch_start + self._ENCODE_MISSING_BATCH]
            flat_texts: list[str] = []
            layout: list[tuple[str, int, int]] = []
            for doc_id, text in batch:
                if isinstance(text, list):
                    texts = [t for t in text if t]
                    if not texts:
                        continue
                    layout.append((doc_id, len(flat_texts), len(texts)))
                    flat_texts.extend(texts)
                elif text:
                    layout.append((doc_id, len(flat_texts), 1))
                    flat_texts.append(text)
            if not flat_texts:
                continue
            vecs = self._encoder.encode_documents(flat_texts, spec.kind, model_key=spec.model_key)
            if vecs is None:
                break  # Encoder unavailable; leave the remainder for the next resume.
            rows = [(doc_id, vecs[start:start + count]) for doc_id, start, count in layout]
            if before_write is not None:
                before_write()
            self._store.upsert_vectors(projection, rows)
            encoded += len(rows)
            batches += 1
        # The scheduled path deliberately never inferred completion from its
        # truncated Python work-list. Keep the exact count in SQLite, where it does
        # not allocate/JSON-decode the entire backlog in the sidecar process.
        remaining = self._store.missing_vector_count(pname, projection)
        return encoded, batches, remaining
