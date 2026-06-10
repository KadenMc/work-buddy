"""Partition — the consolidated index's "source" PORT (a Protocol) + a registry.

A partition is a domain adapter that produces ``Document``s (knowledge units, vault
chunks, conversation spans, …). The index defines the *shape*; domains implement and
register it (``domain → index``, never the reverse — F-PLACEMENT). The registry holds
lazy factories so the engine never imports a domain at module load.

Optional methods (``projection_schema``, ``hydrate``) are accessed via safe module-level
helpers — IR's ``get_projection_schema`` pattern — so a partition that omits them keeps
working (no base class, no forced overrides).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Iterable, Protocol, runtime_checkable

from work_buddy.index.model import ProjectionSpec

if TYPE_CHECKING:
    from work_buddy.index.model import Document, Hit, ItemRef


@runtime_checkable
class Partition(Protocol):
    """A registered source of Documents for one partition of the consolidated index."""

    name: str
    # "hash" (precise; default) | "mtime" — drives change detection (fork F-HASH).
    change_key: str

    def field_weights(self) -> dict[str, float]:
        """BM25 field-importance hints (advisory; the store maps to FTS tiers)."""
        ...

    def discover(self) -> "Iterable[ItemRef]":
        """All indexable source items + their change-detection signal."""
        ...

    def parse(self, item_id: str) -> "list[Document]":
        """Parse one source item into one or more Documents."""
        ...

    # OPTIONAL (accessed via the helpers below, not part of the structural contract):
    #   def projection_schema(self) -> dict[str, ProjectionSpec]: ...
    #   def hydrate(self, hits: list[Hit], **opts) -> list[Any]: ...


def get_projection_schema(partition: Any) -> dict[str, ProjectionSpec]:
    """A partition's dense-projection schema, or ``{}`` if it declares none."""
    getter = getattr(partition, "projection_schema", None)
    if getter is None:
        return {}
    try:
        return getter() or {}
    except Exception:
        return {}


def get_change_key(partition: Any) -> str:
    return getattr(partition, "change_key", "hash") or "hash"


def hydrate(partition: Any, hits: "list[Hit]", **opts: Any) -> list[Any]:
    """Domain-shape the ranked hits (e.g. tier knowledge units). Default: the hits."""
    fn = getattr(partition, "hydrate", None)
    if fn is None:
        return hits
    return fn(hits, **opts)


class PartitionRegistry:
    """Lazy factory registry. Domains register a ``() -> Partition`` factory; the
    instance is built (and cached) on first ``get``."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Partition]] = {}
        self._instances: dict[str, Partition] = {}

    def register(self, name: str, factory: "Callable[[], Partition]") -> None:
        self._factories[name] = factory
        self._instances.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._factories)

    def get(self, name: str) -> "Partition":
        if name not in self._factories:
            raise KeyError(f"Unknown partition: {name!r}. Registered: {self.names()}")
        if name not in self._instances:
            self._instances[name] = self._factories[name]()
        return self._instances[name]

    def all(self) -> "list[Partition]":
        return [self.get(n) for n in self.names()]


_REGISTRY = PartitionRegistry()


def get_partition_registry() -> PartitionRegistry:
    """Module-global partition registry (one per process)."""
    return _REGISTRY


def register_partition(name: str, factory: "Callable[[], Partition]") -> None:
    """Domains call this at import time to register their partition (lazily)."""
    _REGISTRY.register(name, factory)
