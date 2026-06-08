"""Unit tests for ``registry.reload_capability_data`` — the data-only reload.

The whole reason this function exists is that ``invalidate_registry`` (the old
``mcp_registry_reload`` op) purges ``work_buddy.*`` from ``sys.modules``, which
spawns a second class generation and breaks the long-lived FastMCP gateway. The
data-only reload must rebuild the registry from fresh store data WITHOUT that
purge. The core regression these tests lock is exactly that: **no sys.modules
purge.** See ``.data/designs/mcp-registry-reload`` and ``dev/mcp-reload``.
"""

from __future__ import annotations

import sys
import types

import pytest

from work_buddy.knowledge import store
from work_buddy.mcp_server import registry


@pytest.fixture(autouse=True)
def _preserve_singletons():
    """Snapshot + restore the module-level caches so a test's rebuild (real or
    faked) does not leak into sibling tests."""
    saved_reg = registry._REGISTRY
    saved_store = store._STORE
    saved_vault = store._VAULT_STORE
    yield
    registry._REGISTRY = saved_reg
    store._STORE = saved_store
    store._VAULT_STORE = saved_vault


def _install_sentinel() -> str:
    """Register a dummy module under the ``work_buddy.*`` prefix that
    ``invalidate_registry`` WOULD purge. Returns its name."""
    name = "work_buddy.__reload_test_sentinel__"
    sys.modules[name] = types.ModuleType(name)
    return name


def test_reload_capability_data_does_not_purge_sys_modules(monkeypatch):
    """The defining invariant: the data-only reload rebuilds in place and leaves
    ``work_buddy.*`` modules in ``sys.modules`` untouched. ``_build_registry`` is
    faked so the assertion is fast and isolated from real store/tool state."""
    calls = {"build": 0}

    def fake_build():
        calls["build"] += 1
        return {"sentinel_cap": object()}

    monkeypatch.setattr(registry, "_build_registry", fake_build)

    sentinel = _install_sentinel()
    try:
        result = registry.reload_capability_data()

        # The point of the whole feature: no purge.
        assert sentinel in sys.modules, "reload_capability_data purged a work_buddy.* module"

        # A rebuild was actually triggered (cache was dropped, not just read).
        assert calls["build"] == 1
        assert registry._REGISTRY is not None

        # Return contract.
        assert result == {"status": "ok", "entries": 1}
    finally:
        sys.modules.pop(sentinel, None)


def test_reload_capability_data_rereads_store(monkeypatch):
    """It resets the store cache so the next load re-reads disk (this is how
    edited declarations / new workflows are picked up)."""
    invalidated = {"store": False}

    def fake_invalidate_store():
        invalidated["store"] = True
        store._STORE = None
        store._VAULT_STORE = None

    monkeypatch.setattr(store, "invalidate_store", fake_invalidate_store)
    monkeypatch.setattr(registry, "_build_registry", lambda: {"x": object()})

    # Prime the store cache so we can observe it being cleared.
    store._STORE = {"stale": object()}
    registry.reload_capability_data()

    assert invalidated["store"] is True
    assert store._STORE is None


# NOTE: end-to-end coverage (a real _build_registry against the on-disk store,
# and that the ``reload_capability_data`` declaration itself resolves) lives in
# tests/unit/test_capability_declarations_invariant.py + test_registry_invariants.py,
# which build the real registry over ALL declarations. The authoritative
# no-restart behaviour is proven by the live-gateway validation (see
# dev/mcp-reload + .data/designs/mcp-registry-reload). Keeping this file pure-unit
# (no filesystem, no background index thread) per the repo's marker taxonomy.
