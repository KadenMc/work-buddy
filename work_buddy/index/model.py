"""Value types for the consolidated index — domain-agnostic, lifted from the IR
engine's ``ir/sources/base.py`` data model and extended for the consolidation.

This module imports NOTHING from any work-buddy domain package (knowledge, vault,
ir, …). Partitions are registered *into* the index by their domains; the arrow is
always ``domain → index``, never the reverse. See
``.data/designs/index-consolidation/CLASS-ARCHITECTURE.md`` §2.

Design notes
------------
- ``ProjectionKind`` / ``PoolStrategy`` are ``(str, Enum)`` (not ``StrEnum``) so they
  are string-equal to the IR engine's ``Literal`` values (``"label"``, ``"passage"``,
  ``"none"``/``"max"``/``"mean"``) — full interop with ``ir/sources/*`` and JSON, with
  no Python-3.11 floor.
- ``Document`` renames IR's ``source`` to ``partition`` and drops the legacy
  ``dense_text`` scalar; dense views live only in ``projections``. IR-source partition
  wrappers adapt a bare ``dense_text`` into a single ``"default"`` PASSAGE projection.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

# 16 hex chars = 8 bytes of SHA-256 prefix — same scheme as
# ``knowledge/persistence.py::content_hash`` (defined here to keep index/ domain-free).
_HASH_LEN = 16


def content_hash(text: str) -> str:
    """Deterministic 16-char SHA-256 prefix of ``text`` — change-detection key."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:_HASH_LEN]


def make_doc_id(partition: str, stable_id: str) -> str:
    """The globally-unique doc id scheme (fork F-IDENTITY): ``"{partition}:{stable_id}"``."""
    return f"{partition}:{stable_id}"


class ProjectionKind(str, Enum):
    """How a dense projection is encoded on both sides of the comparison.

    - ``LABEL``: short identifying text (alias, title) — symmetric ``leaf-mt`` on doc
      AND query side (peer-to-peer match).
    - ``PASSAGE``: longer body text — asymmetric (``leaf-ir`` doc side, ``leaf-ir-query``
      query side).
    """

    LABEL = "label"
    PASSAGE = "passage"


class PoolStrategy(str, Enum):
    """Per-doc aggregation for a list-valued projection (e.g. many aliases → one score)."""

    NONE = "none"
    MAX = "max"
    MEAN = "mean"


@dataclass(frozen=True)
class ProjectionSpec:
    """A partition's declaration of one named dense projection (once per partition).

    ``model_key`` overrides the encoder model; when ``None`` the router derives it from
    ``kind`` (LABEL→symmetric, PASSAGE→asymmetric). Wires the per-partition model choice
    (fork F-RECENCY/MODEL).
    """

    kind: ProjectionKind
    pool: PoolStrategy = PoolStrategy.NONE
    model_key: str | None = None


@dataclass
class Projection:
    """A single document's value for one named projection (scalar or list-for-pooling)."""

    text: str | list[str]


@dataclass
class Document:
    """The universal indexable record.

    ``fields`` feed BM25 (named, per-field weighted). ``projections`` are the dense
    views, keyed to the partition's ``projection_schema()``. ``metadata`` is
    ``json_extract``-filterable (carries kind/path/scope/vault_id/…). ``content_hash``
    drives change detection; ``timestamp`` (epoch seconds) drives optional recency.
    """

    doc_id: str
    partition: str
    fields: dict[str, str]
    display_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    projections: dict[str, Projection] = field(default_factory=dict)
    content_hash: str = ""
    timestamp: float | None = None

    def ensure_hash(self) -> str:
        """Populate ``content_hash`` from fields+projections if unset; return it."""
        if not self.content_hash:
            self.content_hash = content_hash(self.hashable_text())
        return self.content_hash

    def hashable_text(self) -> str:
        """Stable concatenation of everything that should trigger a re-embed on change."""
        parts: list[str] = [self.partition]
        for k in sorted(self.fields):
            parts.append(f"{k}={self.fields[k]}")
        for k in sorted(self.projections):
            t = self.projections[k].text
            parts.append(f"{k}~{t if isinstance(t, str) else '|'.join(t)}")
        return "\n".join(parts)


@dataclass(frozen=True)
class ItemRef:
    """One discoverable source item: an id + change-detection signal(s).

    Partitions whose ``change_key`` is ``"mtime"`` populate ``mtime``; those using
    ``"hash"`` populate ``content_hash`` (the precise default — fork F-HASH).
    """

    item_id: str
    mtime: float = 0.0
    content_hash: str | None = None


@dataclass(frozen=True)
class Query:
    """A search request against one or more partitions."""

    text: str
    top_k: int = 10
    method: Literal["hybrid", "lexical", "dense"] = "hybrid"
    filters: dict[str, Any] = field(default_factory=dict)
    scope: str | None = None
    recency: bool = False
    rrf_k: int | None = None  # per-call override; else the partition's configured rrf_k
    # Whether retained-but-source-gone docs (``lifecycle_state="orphaned"``, from the
    # "retain"/"ttl" retention modes) are eligible. Default True = include them (recall is
    # the point of retain). False → a "live-only" view that excludes frozen snapshots.
    include_orphaned: bool = True


@dataclass
class Hit:
    """A ranked result: id + fused score + the per-signal breakdown (observability)."""

    doc_id: str
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    display_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
