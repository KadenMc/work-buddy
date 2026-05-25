"""Per-domain registry of `TreeDrillable` instances.

A `TreeDrillable` registers itself by domain name; the dispatch capability
(`drill_tree`) looks up the instance and routes the call. The registry is
lazy: instances are constructed on first access (cheap; the constructors
do no I/O), so importing this module is safe in the gateway boot path.
"""

from __future__ import annotations

from typing import Callable

from work_buddy.disclosure.protocol import TreeDrillable

# Domain → factory. The factory is called once per process the first time
# that domain is requested.
_FACTORIES: dict[str, Callable[[], TreeDrillable]] = {}
_INSTANCES: dict[str, TreeDrillable] = {}


def register_drillable(
    domain: str,
    factory: Callable[[], TreeDrillable],
) -> None:
    """Register a `TreeDrillable` factory under `domain`."""
    _FACTORIES[domain] = factory
    # Invalidate any previously cached instance (lets a domain be
    # re-registered with new state, e.g. in tests).
    _INSTANCES.pop(domain, None)


def get_drillable(domain: str) -> TreeDrillable:
    """Return the `TreeDrillable` for `domain`, lazily constructing once."""
    if domain in _INSTANCES:
        return _INSTANCES[domain]
    factory = _FACTORIES.get(domain)
    if factory is None:
        raise KeyError(
            f"No TreeDrillable registered for domain {domain!r}. "
            f"Available: {sorted(_FACTORIES.keys())}"
        )
    inst = factory()
    _INSTANCES[domain] = inst
    return inst


def available_domains() -> list[str]:
    """Return the list of registered domain names, sorted."""
    return sorted(_FACTORIES.keys())


def _reset_for_tests() -> None:
    """Test-only hook: clear the registry."""
    _FACTORIES.clear()
    _INSTANCES.clear()


# ---------------------------------------------------------------------------
# Default registrations
# ---------------------------------------------------------------------------


def _register_defaults() -> None:
    """Register the built-in domains. Lazy-imported factories avoid
    pulling backends into the import path until first use."""

    def _summary_factory() -> TreeDrillable:
        from work_buddy.disclosure.summary_tree import SummaryTreeDrillable

        return SummaryTreeDrillable()

    def _knowledge_factory() -> TreeDrillable:
        from work_buddy.disclosure.knowledge_tree import KnowledgeTreeDrillable

        return KnowledgeTreeDrillable()

    register_drillable("summary", _summary_factory)
    register_drillable("knowledge", _knowledge_factory)


_register_defaults()
