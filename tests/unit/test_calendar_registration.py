"""Calendar read ops register, declarations resolve, config block loads."""

from __future__ import annotations

import pytest

_READ_OPS = [
    "op.wb.calendar_health",
    "op.wb.calendar_list_events",
    "op.wb.calendar_get_event",
    "op.wb.calendar_coverage",
]


@pytest.fixture
def loaded_ops():
    """Faithfully reload the built-in ops into a clean registry.

    Mirrors the established op-registry test pattern (clear_ops + load_builtin_ops)
    so reloading every ops module re-registers into an empty registry rather
    than tripping the duplicate-registration guard.
    """
    from work_buddy.mcp_server import op_registry

    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    yield op_registry
    op_registry.clear_ops()


def test_calendar_ops_registered(loaded_ops):
    for op_id in _READ_OPS:
        assert callable(loaded_ops.get_op(op_id)), f"{op_id} not registered"


def test_calendar_ops_resolve_via_loader(loaded_ops):
    """Each calendar capability declaration's op resolves to a registered
    callable (the registry pairs the kind:capability unit's `op` with an Op)."""
    try:
        from work_buddy.knowledge.capability_loader import load_declared_capabilities
    except Exception:
        pytest.skip("capability_loader unavailable in this environment")

    declared, _issues = load_declared_capabilities()
    by_name = {c.name: c for c in declared}
    for cap_name in ("calendar_health", "calendar_list_events",
                     "calendar_get_event", "calendar_coverage"):
        assert cap_name in by_name, f"{cap_name} declaration not loaded"
        assert loaded_ops.get_op(f"op.wb.{cap_name}") is not None


def test_calendar_capabilities_require_provider_aware_probe(loaded_ops):
    """Capabilities gate on the provider-aware ``calendar`` probe, not the
    Obsidian-bound ``google_calendar`` one — so they stay available under any
    configured provider (e.g. google_native with Obsidian closed)."""
    try:
        from work_buddy.knowledge.capability_loader import load_declared_capabilities
    except Exception:
        pytest.skip("capability_loader unavailable in this environment")
    declared, _ = load_declared_capabilities()
    by_name = {c.name: c for c in declared}
    for cap_name in ("calendar_list_events", "calendar_coverage",
                     "create_calendar_event", "delete_calendar_event"):
        cap = by_name.get(cap_name)
        assert cap is not None
        assert "calendar" in (cap.requires or [])
        assert "google_calendar" not in (cap.requires or [])


def test_example_config_has_calendar_block():
    import pathlib
    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    assert "calendar" in cfg
    assert cfg["calendar"]["enabled"] is True
    assert cfg["calendar"]["provider"] == "obsidian_bridge"
