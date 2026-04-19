"""Base types for IR data sources.

Every data source produces Documents — the universal unit the IR engine
operates on. The engine doesn't know about conversations, vault notes, or
web pages — it indexes and retrieves Documents. Each source adapter handles
domain-specific parsing and field extraction.

A document carries two independent retrieval signals:

- **fields**: named text chunks fed to BM25 with per-field weights. Multiple
  fields exist so different parts of a doc (title vs body) can be weighted
  differently during lexical scoring.
- **projections**: named dense views of the document, each encoded with a
  strategy appropriate to its text kind. Multiple projections exist so
  different parts of a doc can be encoded differently — e.g. a short
  label-shaped task line uses a symmetric encoder, while a passage-shaped
  note body uses an asymmetric document encoder. At query time the engine
  scores each projection independently and RRF-fuses with BM25.

Legacy back-compat: sources that only set ``dense_text`` (no projections and
no projection schema) continue to work — the engine treats ``dense_text`` as
an implicit single ``"default"`` projection of kind ``passage``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# Text kinds. Each kind tells the engine how to encode both sides of the
# comparison. A source author picks the kind that matches the text's
# function, and the engine routes to the right model.
#
# - "label": short identifying text (task line, alias, title). Encoded with
#   the symmetric ``leaf-mt`` model on both the document and query side —
#   peer-to-peer matching. Good when a hit is "this text *is* what you're
#   asking about", not "this text *contains* what you're asking about".
# - "passage": longer body text (note body, conversation span, doc content).
#   Encoded asymmetrically — document side via ``leaf-ir``, query side via
#   ``leaf-ir-query``. Good for retrieving passages that contain the answer
#   to a shorter query.
ProjectionKind = Literal["label", "passage"]

# Pool strategies for projections whose text is a list (one logical doc,
# multiple strings — e.g. aliases). "none" requires scalar text.
PoolStrategy = Literal["none", "max", "mean"]


@dataclass(frozen=True)
class ProjectionSpec:
    """Source-level declaration of a named dense projection.

    A Source returns a ``{name: ProjectionSpec}`` dict from
    ``projection_schema()``. The spec tells the engine, once per source, how
    every document's projection of that name should be encoded and (if the
    text is a list) pooled at query time.
    """

    kind: ProjectionKind
    pool: PoolStrategy = "none"


@dataclass
class Projection:
    """A single document's value for one named projection.

    Pairs a named chunk of text (or list of texts, for pooled projections)
    with the projection key declared in the source's schema. The engine
    looks up the kind/pool via the schema — not stored on the projection
    itself — so that a schema change is one edit, not N.
    """

    text: str | list[str]


@dataclass
class Document:
    """A single indexable unit for the IR engine.

    ``fields`` are named text segments used for BM25 fielded retrieval
    (e.g. ``title``, ``body`` for task notes). ``projections`` are named
    dense views used for vector retrieval; the set of valid keys is
    declared by the source's ``projection_schema()``.

    ``dense_text`` is kept for back-compat with single-projection sources
    that predate the schema concept: when a source declares no projections
    and a doc has no ``projections`` entries, ``dense_text`` is treated as
    an implicit ``"default"`` projection of kind ``passage``.
    """

    doc_id: str  # Globally unique (e.g. "{session_id}:{span_index}")
    source: str  # Source type key (e.g. "conversation", "task_note")
    fields: dict[str, str]  # Named text fields for BM25
    dense_text: str  # Combined text for vector encoding (legacy single-projection)
    display_text: str  # Human-readable preview for results
    metadata: dict[str, Any] = field(default_factory=dict)
    projections: dict[str, Projection] = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    """Protocol for IR data source adapters."""

    @property
    def name(self) -> str:
        """Source type key (e.g. 'conversation')."""
        ...

    def discover(self, days: int = 30) -> list[str]:
        """Return IDs of indexable items (e.g. session file paths).

        Only items modified within the lookback window should be returned.
        """
        ...

    def parse(self, item_id: str) -> list[Document]:
        """Parse a single item into one or more Documents."""
        ...

    def default_field_weights(self) -> dict[str, float]:
        """Default BM25 field weights for this source."""
        ...

    # NOTE: sources MAY implement ``projection_schema() -> dict[str, ProjectionSpec]``
    # to declare one or more named dense projections. It's not part of the
    # protocol because existing sources don't implement it and shouldn't be
    # forced to; use ``get_projection_schema(source)`` at the engine for
    # safe access. Returning no schema (or no method at all) keeps the
    # legacy single-projection path: ``dense_text`` is encoded once as an
    # implicit ``"default"`` projection of kind ``passage``.


def get_projection_schema(source: Source) -> dict[str, ProjectionSpec]:
    """Safe accessor for a source's projection schema.

    Existing sources (conversation, docs, chrome, projects) don't implement
    ``projection_schema()`` at all — that's fine, they stay on the legacy
    single-projection path. This helper returns an empty dict in that case
    so the engine has a uniform call site without every adapter needing
    a no-op implementation.
    """
    getter = getattr(source, "projection_schema", None)
    if getter is None:
        return {}
    return getter() or {}
