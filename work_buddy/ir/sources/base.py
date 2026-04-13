"""Base types for IR data sources.

Every data source produces Documents — the universal unit the IR engine
operates on. The engine doesn't know about conversations, vault notes, or
web pages — it indexes and retrieves Documents. Each source adapter handles
domain-specific parsing and field extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Document:
    """A single indexable unit for the IR engine.

    Fields are named text segments used for BM25 fielded retrieval
    (e.g. user_text, assistant_text, tool_names for conversations).
    dense_text is the combined text sent to the embedding model.
    """

    doc_id: str  # Globally unique (e.g. "{session_id}:{span_index}")
    source: str  # Source type key (e.g. "conversation", "vault")
    fields: dict[str, str]  # Named text fields for BM25
    dense_text: str  # Combined text for vector encoding
    display_text: str  # Human-readable preview for results
    metadata: dict[str, Any] = field(default_factory=dict)


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
