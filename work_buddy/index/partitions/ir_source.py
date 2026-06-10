"""IRSourcePartition — adapt an existing IR ``Source`` into an index ``Partition``.

ONE generic wrapper covers every IR source (conversation, projects, chrome, summary,
task_note) by delegating to its ``discover``/``parse``/``default_field_weights``/
``projection_schema`` and converting IR ``Document``s into index ``Document``s:
``source`` → ``partition``, a bare ``dense_text`` → a single ``content`` PASSAGE
projection, and ISO ``start_time``/``end_time`` metadata → an epoch ``timestamp`` (so
recency works). Reuses the live ``ir/sources/*`` — does not rewrite them.

Two OPTIONAL, source-interpreted capabilities make the wrapper extensible to arbitrary
"history" sources without per-source code here (see HISTORY-PARTITION-COVERAGE.md):

- **Coverage** — if the source's ``discover`` accepts a ``coverage`` kwarg, the adapter
  forwards the partition's configured coverage (``"active"`` default / ``"all"``). A
  source that doesn't accept it is unaffected. This is what lets a history source admit
  archived/closed items into the corpus so retrospective queries can find them.
- **Lifecycle** — if the source exposes ``lifecycle(item_ids) -> {id: state}``, the
  adapter (a) folds the current state into the change-detection token so an
  archive/complete transition re-indexes the item even when its file mtime is unchanged
  (``change_key`` becomes ``"hash"``), and (b) stamps a uniform ``lifecycle_state``
  metadata key so ANY source's states are filterable by ANY query with one key.

Both are duck-typed (``getattr``/signature introspection), so a brand-new source opts in
by implementing them and is otherwise byte-identical to today.

NOTE: the IR ``docs`` source is intentionally NOT wrapped — it's the redundant second
index of the knowledge store, replaced by ``KnowledgePartition``.
"""

from __future__ import annotations

import inspect
from typing import Any

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

# IR sources to expose as partitions (everything except the redundant "docs").
_IR_PARTITIONS = ("conversation", "projects", "chrome", "summary", "task_note")


def _accepts(fn: Any, param: str) -> bool:
    """True if callable ``fn`` accepts a keyword parameter named ``param``."""
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


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

    def __init__(self, ir_source: Any, *, coverage: str = "active") -> None:
        self._src = ir_source
        self.name = ir_source.name
        self._coverage = coverage or "active"
        self._has_lifecycle = callable(getattr(ir_source, "lifecycle", None))
        # A source with a lifecycle has out-of-file mutable state (archive/complete),
        # so mtime alone misses transitions → use a hash token that folds state in.
        self.change_key = "hash" if self._has_lifecycle else "mtime"
        self._states: dict[str, str] = {}

    def configure(self, cfg: Any) -> None:
        """Apply per-partition config (called by ``IndexPartition``). Reads ``coverage``.

        Optional hook — uses the config the facade was constructed with (not global),
        so the A/B harness and tests can drive coverage explicitly.
        """
        cov = getattr(cfg, "coverage", None)
        if cov:
            self._coverage = cov

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
        # Forward coverage only when the source understands it (else byte-identical
        # to today). Keep the existing `days`-fallback for sources that require it.
        disc = self._src.discover
        kwargs: dict[str, Any] = {}
        if self._coverage and self._coverage != "active" and _accepts(disc, "coverage"):
            kwargs["coverage"] = self._coverage
        try:
            raw = disc(**kwargs)
        except TypeError:
            raw = disc(days=3650, **kwargs)  # some sources require a lookback arg

        pairs: list[tuple[str, float]] = []
        for entry in raw or []:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                pairs.append((str(entry[0]), float(entry[1])))
            else:
                pairs.append((str(entry), 0.0))

        # Lifecycle enrichment: capture current state for change detection (token) and
        # for the parse-time `lifecycle_state` stamp. Best-effort — a failure degrades
        # to mtime-only behavior, never blocks the build.
        self._states = {}
        if self._has_lifecycle:
            try:
                self._states = self._src.lifecycle([iid for iid, _ in pairs]) or {}
            except Exception as exc:
                logger.debug("lifecycle() failed for %s; mtime-only: %s", self.name, exc)
                self._states = {}

        refs: list[ItemRef] = []
        for item_id, mtime in pairs:
            if self._has_lifecycle:
                # Token folds mtime AND state, so a file edit OR a state transition
                # both register as "changed" under hash-mode change detection.
                token = content_hash(f"{mtime}|{self._states.get(item_id, '')}")
                refs.append(ItemRef(item_id=item_id, mtime=mtime, content_hash=token))
            else:
                refs.append(ItemRef(item_id=item_id, mtime=mtime))
        return refs

    def parse(self, item_id: str) -> list[Document]:
        state = self._states.get(item_id) if self._has_lifecycle else None
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
            # Uniform lifecycle key for cross-source query-time filtering (a source's
            # own native key, e.g. task_state, is preserved alongside it).
            if state is not None and "lifecycle_state" not in meta:
                meta["lifecycle_state"] = state
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
