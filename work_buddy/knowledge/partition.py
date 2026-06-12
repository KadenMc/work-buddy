"""KnowledgePartition — the knowledge store as a consolidated-index partition.

Domain-owned (F-PLACEMENT): lives in ``knowledge/`` and registers itself into the
index's partition registry at import time. ``index/`` never imports this; the registry
bootstrap (``index/partitions/bootstrap.py``) imports it to trigger registration.

Preserves the knowledge index's domain fit:
- one Document per unit (whole-unit), ``<<wb:>>``-resolved content;
- a ``content`` PASSAGE projection (asymmetric) + an ``aliases`` LABEL/MAX projection
  (the max-pooled alias signal);
- content-hash change detection;
- ``scope`` metadata (system/personal/all → one partition + a filter, fork F-SCOPE);
- ``hydrate`` → ``KnowledgeUnit.tier(depth)`` so agent_docs-style results are unchanged.
"""

from __future__ import annotations

from typing import Any, Iterable

from work_buddy.index.model import (
    Document,
    ItemRef,
    PoolStrategy,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    content_hash,
    make_doc_id,
)
from work_buddy.index.partition import register_partition
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_PARTITION = "knowledge"


def _spaced(name: str) -> str:
    return (name or "").replace("-", " ").replace("_", " ")


def _aliases(unit: Any) -> list[str]:
    return [a for a in (getattr(unit, "aliases", None) or []) if a and a.strip()]


def _content_text(unit: Any, store: dict | None) -> str:
    """Dense passage text: name + description + tags + summary + (resolved) full body.

    Excludes aliases (they get their own LABEL projection) — matches the old knowledge
    index's ``content_text``.
    """
    from work_buddy.knowledge.model import _resolve_placeholders

    content = getattr(unit, "content", None) or {}
    summary = content.get("summary", "") or ""
    full = content.get("full", "") or summary
    if store is not None and full and "<<wb:" in full:
        try:
            full = _resolve_placeholders(full, store)
        except Exception:  # resolution must never break indexing
            pass
    parts = [_spaced(getattr(unit, "name", "")), getattr(unit, "description", "") or ""]
    tags = list(getattr(unit, "tags", None) or [])
    if tags:
        parts.append(" ".join(tags))
    if summary:
        parts.append(summary)
    if full and full != summary:
        parts.append(full)
    return "\n".join(p for p in parts if p)


class KnowledgePartition:
    name = _PARTITION
    change_key = "hash"

    def __init__(self, store_loader=None) -> None:
        # store_loader: () -> dict[path, KnowledgeUnit]; default load_store(scope="all").
        self._store_loader = store_loader

    def _store(self) -> dict:
        if self._store_loader is not None:
            return self._store_loader()
        from work_buddy.knowledge.store import load_store
        return load_store(scope="all")

    def field_weights(self) -> dict[str, float]:
        return {"name": 3.0, "tags": 2.0, "body": 1.0}

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        return {
            "content": ProjectionSpec(kind=ProjectionKind.PASSAGE),
            "aliases": ProjectionSpec(kind=ProjectionKind.LABEL, pool=PoolStrategy.MAX),
        }

    def discover(self) -> Iterable[ItemRef]:
        store = self._store()
        refs: list[ItemRef] = []
        for path, unit in store.items():
            ct = _content_text(unit, store)
            aliases = _aliases(unit)
            h = content_hash(ct + "\x01" + "\x01".join(aliases))
            refs.append(ItemRef(item_id=path, content_hash=h))
        return refs

    def parse(self, item_id: str) -> list[Document]:
        store = self._store()
        unit = store.get(item_id)
        if unit is None:
            return []
        return [self._to_document(item_id, unit, store)]

    def _to_document(self, path: str, unit: Any, store: dict) -> Document:
        from work_buddy.knowledge.model import VaultUnit

        ct = _content_text(unit, store)
        aliases = _aliases(unit)
        tags = list(getattr(unit, "tags", None) or [])
        kind = getattr(unit, "kind", "") or ""
        scope = "personal" if isinstance(unit, VaultUnit) else "system"
        description = getattr(unit, "description", "") or ""
        # VaultUnit-only filter fields ("" for system units) — indexed so agent_docs
        # category/severity filters can be PUSHED DOWN (filter-then-rank) instead of
        # post-filtered from a bounded pool.
        category = getattr(unit, "category", "") or ""
        severity = getattr(unit, "severity", "") or ""

        # Body for lexical recall = full content text + aliases (title=name weighted higher).
        body = ct + (("\n" + " ".join(aliases)) if aliases else "")
        fields = {
            "name": _spaced(getattr(unit, "name", "")),
            "tags": " ".join(tags),
            "body": body,
        }
        projections: dict[str, Projection] = {"content": Projection(text=ct)}
        if aliases:
            projections["aliases"] = Projection(text=aliases)

        return Document(
            doc_id=make_doc_id(_PARTITION, path),
            partition=_PARTITION,
            fields=fields,
            display_text=f"[{kind}] {path}: {description}",
            metadata={
                "kind": kind, "path": path, "scope": scope, "tags": tags,
                "category": category, "severity": severity,
            },
            projections=projections,
        )

    def hydrate(self, hits: list, *, depth: str = "index", dev: bool = False, **opts) -> list[Any]:
        """Map ranked hits → depth-tiered units (agent_docs-shaped results)."""
        store = self._store()
        out: list[Any] = []
        for h in hits:
            path = h.doc_id.split(":", 1)[1] if ":" in h.doc_id else h.doc_id
            unit = store.get(path)
            if unit is None:
                continue
            try:
                tiered = unit.tier(depth, store=store, dev=dev)
            except Exception as exc:  # tiering must not break a result
                logger.debug("knowledge hydrate tier(%s) failed: %s", path, exc)
                tiered = {"name": getattr(unit, "name", path)}
            out.append({"path": path, "score": h.score, **tiered})
        return out


register_partition(_PARTITION, lambda: KnowledgePartition())
