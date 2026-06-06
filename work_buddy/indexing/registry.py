"""Name → :class:`Index` factory (mirrors ``ir/store.py::_get_source``).

Each adapter is imported lazily so importing the registry doesn't drag every
backend (and its heavy deps) into the importer's process.
"""
from __future__ import annotations

from work_buddy.indexing.protocol import Index


def _ir() -> Index:
    from work_buddy.indexing.adapters.ir import IRIndexAdapter
    return IRIndexAdapter()


def _vault() -> Index:
    from work_buddy.indexing.adapters.vault import VaultIndexAdapter
    return VaultIndexAdapter()


def _knowledge() -> Index:
    from work_buddy.indexing.adapters.knowledge import KnowledgeIndexAdapter
    return KnowledgeIndexAdapter()


_FACTORIES = {
    "ir": _ir,
    "vault_index": _vault,
    "knowledge": _knowledge,
}


def index_names() -> list[str]:
    """The registered index names, in display order."""
    return list(_FACTORIES)


def get_index(name: str) -> Index:
    """Instantiate the adapter for ``name``. Raises ``ValueError`` if unknown."""
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown index: {name}. Available: {', '.join(sorted(_FACTORIES))}"
        )
    return factory()
