"""Protocols, datatypes, and exceptions for the summarization framework.

`Summarizer = Source × Strategy × Store` composition. Three pluggable axes
conforming to `typing.Protocol`s, plus uniform provenance stamping (assembled
by the orchestrator from the strategy + store + LLM response — not a
pluggable axis here, unlike the artifact system where it genuinely varies).

The stored summary is always a `SummaryNode` tree:

- **Flat** extraction = depth-1 (a single root, empty `children`, null
  `source_ref`).
- **Layered** disclosure = root + child nodes, each child carrying a
  `source_ref` pointer to exact source events.

Every node has a `source_ref` slot even though flat extraction never uses it.
This is load-bearing: it lets the deferred progressive-disclosure phase fold
in additively (adds *consumers* of the tree, never reshapes the stored row).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclass
class SummaryNode:
    """One node in a summary tree — the universal stored shape."""

    summary: str
    source_ref: Any | None = None
    children: list["SummaryNode"] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def is_leaf(self) -> bool:
        return not self.children

    def walk(self) -> Iterable[tuple[int, "SummaryNode"]]:
        """Pre-order traversal yielding `(level, node)`. Root is level 0."""
        yield 0, self
        for child in self.children:
            for sub_level, sub_node in child.walk():
                yield sub_level + 1, sub_node


@dataclass
class DiscoveryWindow:
    """Bounds and mode for a discovery pass.

    `days` is advisory — sources that don't have a time dimension (e.g. Chrome
    tab summarization, where the caller supplies an explicit item set) ignore
    it. `max_items` caps per-call work; `force` bypasses staleness filtering.
    """

    days: int = 7
    max_items: int = 3
    force: bool = False


@dataclass(frozen=True)
class Provenance:
    """Uniform provenance stamping, assembled by the orchestrator core.

    `prompt_version` and `summary_schema_version` come from the Strategy;
    `selection_version` and `cache_version` come from the Store; `model`,
    `backend`, `profile` are read off the LLM response (or callsite).
    """

    model: str | None
    backend: str | None
    profile: str | None
    generated_at: str
    prompt_version: int
    summary_schema_version: int
    selection_version: int
    cache_version: int

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


class SummaryCapability(str, Enum):
    """Capabilities declared by the three axes.

    The Summarizer composer validates coherent combinations at construction
    time (e.g. a `LAYERED` strategy paired with a `PERSISTS_FLAT`-only store
    is incoherent).
    """

    # Strategy: output shape
    LAYERED = "layered"
    FLAT = "flat"

    # Source + Strategy together: batch path
    BATCHED = "batched"

    # Strategy: incremental refresh (v2). Routes through the orchestrator's
    # incremental path: load prior topics → re-feed only fresh tail + compressed
    # prior topics → merge result with finalized topics in the store. Composed
    # with LAYERED today; could compose with FLAT in the future.
    INCREMENTAL = "incremental"

    # Store: persistence shape
    PERSISTS_TREE = "persists_tree"
    PERSISTS_FLAT = "persists_flat"

    # Store: staleness model
    VERSION_STAMPED = "version_stamped"
    TTL_EVICTED = "ttl_evicted"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IncoherentComposition(ValueError):
    """Raised at `Summarizer` construction when the three axes are mismatched.

    Examples: a layered strategy paired with a store that can only persist a
    flat record; a batched strategy paired with a non-batched source.
    """


class SummarizationError(RuntimeError):
    """Per-item summarization failure.

    The orchestrator catches this and isolates the failure: the item's row
    flips to `status='error'`, prior good results are preserved, and other
    items in the same refresh pass continue.
    """


# ---------------------------------------------------------------------------
# Axis protocols
# ---------------------------------------------------------------------------


class Source(Protocol):
    """Domain adapter — enumerates items and renders them to prompt text.

    `render_batch` is only required if `BATCHED` is in `capabilities`; the
    orchestrator dispatches per-capability. Freshness tokens are opaque to the
    source — the store stringifies them for staleness comparison.
    """

    name: str
    capabilities: frozenset[SummaryCapability]

    def discover(self, window: DiscoveryWindow) -> list[tuple[str, Any]]:
        """Return `[(item_id, freshness_token), ...]` for candidates."""
        ...

    def render(self, item_id: str) -> str | None:
        """Render one item's content into prompt text.

        Return `None` if there is nothing to summarize (e.g. an empty session).
        The orchestrator treats `None` as a clean skip — no LLM call, no error.
        """
        ...

    def render_batch(self, item_ids: list[str]) -> list[str | None]:
        """Render multiple items at once. Only required if `BATCHED`.

        Returns a list aligned with `item_ids`; each element is prompt text
        or `None` (skip this item).
        """
        ...


class SummaryStrategy(Protocol):
    """Output-shape adapter — owns the prompt, schema, and parse.

    Two flavors:
    - `LayeredDisclosureStrategy` — tldr + ordered child segments with refs.
    - `FlatExtractionStrategy` — single root with structured extra fields.
    """

    name: str
    prompt_version: int
    schema_version: int
    capabilities: frozenset[SummaryCapability]
    system_prompt: str
    output_schema: dict[str, Any]

    def parse(
        self,
        structured_output: dict[str, Any] | None,
        raw_content: str,
    ) -> SummaryNode:
        """Convert an LLM response into a `SummaryNode` tree.

        Flat strategies return a 1-node tree (root only, empty `children`,
        null `source_ref`). Layered strategies return root + children, each
        child carrying a `source_ref`.

        Raise `SummarizationError` on unparseable response; the orchestrator
        catches and isolates.
        """
        ...

    def parse_batch(
        self,
        structured_output: dict[str, Any] | None,
        raw_content: str,
        item_ids: list[str],
    ) -> list[SummaryNode | None]:
        """Parse a batched response into per-item trees. Only if `BATCHED`.

        Returns a list aligned with `item_ids`. An element may be `None` if
        the LLM omitted that item.
        """
        ...


@runtime_checkable
class Store(Protocol):
    """Persistence + staleness adapter.

    Implementations MUST share one private predicate between `is_fresh` and
    `select_stale` — failing to compare freshness identically in both paths
    creates a "looks fresh on save, looks stale on discover" race.
    """

    name: str
    capabilities: frozenset[SummaryCapability]
    selection_version: int
    cache_version: int

    def is_fresh(self, item_id: str, freshness_token: Any) -> bool: ...

    def select_stale(
        self,
        candidates: list[tuple[str, Any]],
    ) -> list[tuple[str, Any]]:
        """Filter `candidates` down to those whose stored summaries are
        missing or stale relative to the candidate's freshness token."""
        ...

    def save(
        self,
        item_id: str,
        result: SummaryNode,
        provenance: Provenance,
        freshness_token: Any,
    ) -> None: ...

    def load(self, item_id: str) -> SummaryNode | None: ...

    def record_error(
        self,
        item_id: str,
        error: str,
        provenance: Provenance,
    ) -> None:
        """Record that summarization failed for `item_id` without overwriting
        a prior good result. Implementations flip a status flag and keep the
        prior tree (if any) loadable via `load`."""
        ...


# ---------------------------------------------------------------------------
# LLM injection seam
# ---------------------------------------------------------------------------


@dataclass
class LLMCallResult:
    """Normalized LLM response, independent of the underlying client.

    The orchestrator needs five things: structured output, raw content, model
    name, backend name, and an error flag. Anything else is squashed.

    The `as_caller()` adapter (in `orchestrator.py`) wraps legacy
    bare-dict-returning `llm_call=` stubs (used by existing conv_obs tests)
    so they keep working without modification.
    """

    structured_output: dict[str, Any] | None = None
    content: str = ""
    model: str | None = None
    backend: str | None = None
    error: str | None = None

    def is_error(self) -> bool:
        return self.error is not None


class LLMCaller(Protocol):
    """One-method LLM call abstraction used by the orchestrator.

    Default implementation wraps `LLMRunner().call(...)` at
    `ModelTier.FRONTIER_FAST` and reads `.structured_output`. The framework's
    Store is the one responsible for caching — `LLMCaller.call` does NOT
    accept `cache_ttl_minutes`; passing it would double-cache.
    """

    def call(
        self,
        *,
        system: str,
        user: str,
        output_schema: dict[str, Any] | None = None,
        profile: str | None = None,
        max_tokens: int | None = None,
        trace_id: str | None = None,
    ) -> LLMCallResult:
        ...
