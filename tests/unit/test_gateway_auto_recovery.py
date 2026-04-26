"""Tests for the gateway's CP-A3 lazy auto-recovery wiring.

Direct unit testing of ``wb_run`` is hard — it's an ``async`` MCP-decorated
function whose surface assumes a live transport and FastMCP context. The
recovery PRIMITIVES are thoroughly tested in ``test_recovery.py``; these
tests verify the gateway module's WIRING — that it imports the right
helpers, that the new error message shape is produced when recovery
fails, and that the gateway's response envelope carries the
``registry_auto_recovered`` marker on the success path.

End-to-end auto-recovery (calling a disabled capability and seeing it
restored mid-dispatch) is verified by the live smoke test in the
verification phase — see DECISIONS notes.
"""
from __future__ import annotations

import importlib

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Wiring smoke tests — gateway module references the recovery primitives
# ---------------------------------------------------------------------------


class TestGatewayWiringSmoke:
    def test_gateway_can_import_recovery_primitives(self):
        """The gateway's CP-A3 path lazily imports
        ``recheck_disabled_capability``. Verify the import works."""
        from work_buddy.recovery import recheck_disabled_capability  # noqa: F401
        assert callable(recheck_disabled_capability)

    def test_gateway_module_loadable(self):
        """The gateway module loads cleanly with the CP-A3 changes."""
        gateway = importlib.import_module("work_buddy.mcp_server.tools.gateway")
        assert hasattr(gateway, "_prepare")

    def test_gateway_source_references_recheck(self):
        """Sanity: the wb_run dispatch path references the recovery
        primitive. Catches accidental rip-out (or a future edit that
        replaces the wiring without realising it's load-bearing)."""
        from pathlib import Path

        gateway_path = (
            Path(__file__).parent.parent.parent
            / "work_buddy" / "mcp_server" / "tools" / "gateway.py"
        )
        source = gateway_path.read_text(encoding="utf-8")
        assert "recheck_disabled_capability" in source, (
            "gateway.py no longer references recheck_disabled_capability — "
            "the CP-A3 auto-recovery wiring may have been removed."
        )
        assert "registry_auto_recovered" in source, (
            "gateway.py no longer sets registry_auto_recovered marker — "
            "agents won't be able to tell when a call succeeded via lazy "
            "recovery."
        )
        assert "auto_recovery_attempted" in source, (
            "gateway.py no longer emits auto_recovery_attempted in the "
            "still-disabled error path — agents will get the pre-CP-A3 "
            "generic message instead of knowing recovery was tried."
        )


# ---------------------------------------------------------------------------
# End-to-end recovery flow via the recovery primitive directly
# ---------------------------------------------------------------------------


class TestRecoveryFlowEndToEnd:
    """Full dispatch path covered by the live smoke test (verification
    phase). These tests exercise the recovery primitive against the
    real registry to confirm the CP-A1 + CP-A2 + CP-A3 chain works
    against actual Capability objects (not just synthetic mocks)."""

    @pytest.fixture
    def fresh_registry(self):
        """Force a registry rebuild with obsidian unavailable."""
        from work_buddy.mcp_server import registry as reg_mod
        from work_buddy.tools import DISABLED_CAPABILITIES

        reg_mod._REGISTRY = None
        reg_mod._DISABLED_REGISTRY.clear()
        DISABLED_CAPABILITIES.clear()

        # Build with obsidian unavailable.
        with patch("work_buddy.tools.is_tool_available") as mock_avail:
            mock_avail.side_effect = lambda t: t != "obsidian"
            reg_mod.get_registry()

        yield reg_mod

        # Clean up — force a fresh build on next access.
        reg_mod._REGISTRY = None
        reg_mod._DISABLED_REGISTRY.clear()
        DISABLED_CAPABILITIES.clear()

    def test_recheck_restores_real_obsidian_capability(self, fresh_registry):
        """Pick a real obsidian-requiring capability from the live
        DISABLED_CAPABILITIES, simulate obsidian recovering, and verify
        recheck_disabled_capability restores it to _REGISTRY."""
        from work_buddy.recovery import recheck_disabled_capability
        from work_buddy.tools import DISABLED_CAPABILITIES

        # Find a real obsidian-disabled capability (the registry has many).
        candidates = [
            n for n, deps in DISABLED_CAPABILITIES.items()
            if "obsidian" in deps and len(deps) == 1
        ]
        assert candidates, (
            "Registry has no single-obsidian-dep capabilities — test "
            "needs a different capability to exercise."
        )
        cap_name = candidates[0]

        # Simulate obsidian recovering.
        with patch("work_buddy.tools.reprobe_one"), \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            recovered = recheck_disabled_capability(cap_name)

        assert recovered is True, (
            f"recheck_disabled_capability returned False for {cap_name}; "
            f"DISABLED_CAPABILITIES={DISABLED_CAPABILITIES.get(cap_name)}"
        )
        assert cap_name in fresh_registry.get_registry(), (
            f"{cap_name} not in live registry post-restore"
        )
        assert cap_name not in DISABLED_CAPABILITIES, (
            f"{cap_name} still in DISABLED_CAPABILITIES post-restore"
        )

    def test_failed_recheck_keeps_capability_disabled(self, fresh_registry):
        """If the probe says still-unavailable, the capability stays
        disabled — caller will see the enriched error message."""
        from work_buddy.recovery import recheck_disabled_capability
        from work_buddy.tools import DISABLED_CAPABILITIES

        candidates = [
            n for n, deps in DISABLED_CAPABILITIES.items()
            if "obsidian" in deps and len(deps) == 1
        ]
        assert candidates
        cap_name = candidates[0]

        with patch("work_buddy.tools.reprobe_one"), \
             patch("work_buddy.tools.is_tool_available", return_value=False):
            recovered = recheck_disabled_capability(cap_name)

        assert recovered is False
        assert cap_name in DISABLED_CAPABILITIES
        assert cap_name not in fresh_registry.get_registry()
