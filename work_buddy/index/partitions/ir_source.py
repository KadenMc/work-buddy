"""IRSourcePartition — adapt an existing IR ``Source`` into an index ``Partition``.

ONE generic wrapper covers every IR source (conversation, projects, chrome, summary,
task_note) by delegating to its ``discover``/``parse``/``default_field_weights``/
``projection_schema`` and converting IR ``Document``s into index ``Document``s:
``source`` → ``partition``, a bare ``dense_text`` → a single ``content`` PASSAGE
projection, and ISO ``start_time``/``end_time`` metadata → an epoch ``timestamp`` (so
recency works). Reuses the live ``ir/sources/*`` — does not rewrite them.

NOTE: the IR ``docs`` source is intentionally NOT wrapped — it's the redundant second
index of the knowledge store, replaced by ``KnowledgePartition``.
"""

from __future__ import annotations

from typing import Any

from work_buddy.index.model import (
    Document,
    ItemRef,
    PoolStrategy,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    make_doc_id,
)
from work_buddy.index.partition import register_partition
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# IR sources to expose as partitions (everything except the redundant "docs").
_IR_PARTITIONS = ("conversation", "projects", "chrome", "summary", "task_note")


def _to_epoch(meta: dict | None) -> float | None:
    ts = (meta or {}).get("start_time") or (meta or {}).get("end_time")
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class IRSourcePartition:
    """Adapter wrapping one ``ir.sources`` Source as an index Partition."""

    change_key = "mtime"

    def __init__(self, ir_source: Any) -> None:
        self._src = ir_source
        self.name = ir_source.name

    def field_weights(self) -> dict[str, float]:
        try:
            return self._src.default_field_weights() or {}
        except Exception:
            return {}

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        try:
            from work_buddy.ir.sources.base import get_projection_schema
            sch = get_projection_schema(self._src)
        except Exception:
            sch = {}
        if sch:
            out: dict[str, ProjectionSpec] = {}
            for k, spec in sch.items():
                try:
                    out[k] = ProjectionSpec(
                        kind=ProjectionKind(spec.kind), pool=PoolStrategy(spec.pool),
                    )
                except Exception:
                    out[k] = ProjectionSpec(kind=ProjectionKind.PASSAGE)
            return out
        # legacy single-projection sources: dense_text → one PASSAGE projection
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def discover(self):
        try:
            raw = self._src.discover()
        except TypeError:
            raw = self._src.discover(days=3650)  # some sources require a lookback arg
        refs: list[ItemRef] = []
        for entry in raw or []:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                item_id, mtime = entry[0], float(entry[1])
            else:
                item_id, mtime = entry, 0.0
            refs.append(ItemRef(item_id=str(item_id), mtime=mtime))
        return refs

    def parse(self, item_id: str) -> list[Document]:
        out: list[Document] = []
        for d in (self._src.parse(item_id) or []):
            stable = getattr(d, "doc_id", "")
            doc_id = stable if stable.startswith(self.name + ":") else make_doc_id(self.name, stable)
            projections: dict[str, Projection] = {}
            ir_projs = getattr(d, "projections", None) or {}
            if ir_projs:
                for k, p in ir_projs.items():
                    projections[k] = Projection(text=p.text)
            elif getattr(d, "dense_text", ""):
                projections["content"] = Projection(text=d.dense_text)
            meta = dict(getattr(d, "metadata", None) or {})
            out.append(Document(
                doc_id=doc_id,
                partition=self.name,
                fields=dict(getattr(d, "fields", None) or {}),
                display_text=getattr(d, "display_text", "") or "",
                metadata=meta,
                projections=projections,
                timestamp=_to_epoch(meta),
            ))
        return out


def _factory(name: str):
    def make():
        from work_buddy.ir.store import _get_source
        return IRSourcePartition(_get_source(name))
    return make


def register_ir_partitions() -> None:
    """Register every IR source (except ``docs``) as a lazy partition."""
    for name in _IR_PARTITIONS:
        register_partition(name, _factory(name))
