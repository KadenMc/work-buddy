"""IndexBuilder — incremental, resumable, locked build of one partition.

Flow (under a per-partition advisory lock):
  discover → diff by ``change_key`` (content-hash default, or mtime) → for each
  changed item: delete its old docs, parse, upsert, encode projections (BACKGROUND,
  batched across docs) → prune deleted items → if anything changed, bump the partition
  ``build_version`` and invalidate its resident matrices.

Generalizes ``vault_index/indexer.py`` + ``ir/store.build_index`` and adds the advisory
lock the IR build lacked. Resumable: re-encodes any docs still missing vectors.
"""

from __future__ import annotations

import contextlib
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
        residents: "ResidentCacheRegistry | None" = None,
        use_lock: bool = True,
    ) -> None:
        self._store = store
        self._encoder = encoder
        self._partition = partition
        self._use_lock = use_lock
        if residents is None:
            from work_buddy.index.resident import get_registry
            residents = get_registry()
        self._residents = residents

    def _lock_ctx(self):
        if not self._use_lock:
            return contextlib.nullcontext()
        try:
            from work_buddy.utils.index_lock import index_lock
            target = self._store.db_path.parent / f"{self._store.db_path.name}.{self._partition.name}"
            return index_lock(target)
        except Exception as exc:  # lock infra unavailable → proceed without (best-effort)
            logger.debug("index_lock unavailable (%s); building without a lock", exc)
            return contextlib.nullcontext()

    def build(
        self, *, force: bool = False,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict[str, Any]:
        pname = self._partition.name
        change_key = get_change_key(self._partition)
        schema = get_projection_schema(self._partition)

        with self._lock_ctx():
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
            for iid in deleted:
                self._store.delete_item_docs(iid, partition=pname)

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
                self._encode_docs(docs, schema)
                self._store.mark_item_indexed(
                    ref.item_id, pname, mtime=ref.mtime,
                    content_hash=ref.content_hash or "", doc_count=len(docs),
                )
                n_docs += len(docs)
                if on_progress:
                    on_progress({"phase": "indexing", "done": i + 1, "total": len(changed)})

            # --- resume: encode any docs still missing a vector ---
            for proj_name, spec in schema.items():
                self._encode_missing(pname, proj_name, spec)

            changed_any = bool(changed or deleted)
            if changed_any:
                self._store.bump_version(pname)
                from datetime import datetime, timezone
                self._store.set_meta(f"last_build:{pname}", datetime.now(timezone.utc).isoformat())
                for proj_name in schema:
                    self._residents.invalidate(f"{pname}:{proj_name}")

            stats = {
                "partition": pname,
                "changed": len(changed),
                "deleted": len(deleted),
                "docs_indexed": n_docs,
                "doc_count": self._store.doc_count(pname),
                "version": self._store.build_version(pname),
            }
            logger.info("index build [%s]: %s", pname, stats)
            return stats

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

    def _encode_missing(self, pname: str, projection: str, spec) -> None:
        work = self._store.docs_missing_vectors(pname, projection)
        if not work:
            return
        flat_texts: list[str] = []
        layout: list[tuple[str, int, int]] = []
        for doc_id, text in work:
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
            return
        vecs = self._encoder.encode_documents(flat_texts, spec.kind, model_key=spec.model_key)
        if vecs is None:
            return
        rows = [(doc_id, vecs[start:start + count]) for doc_id, start, count in layout]
        self._store.upsert_vectors(projection, rows)
