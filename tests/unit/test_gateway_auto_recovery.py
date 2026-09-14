"""Tests for the gateway's CP-A3 lazy auto-recovery wiring.

Direct unit testing of ``wb_run`` is hard — it's an ``async`` MCP-decorated
function whose surface assumes a live transport and FastMCP context. The
recovery PRIMITIVES are thoroughly tested in ``test_recovery.py``; these
tests verify the gateway module's WIRING — that it imports the right
helpers, that the new error message shape is produced when recovery
fails, and that the gateway's response envelope carries the
``registry_auto_recovered`` marker on the success path.

End-to-end auto-recovery (calling a disabled skill and seeing it
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
        ``recheck_disabled_skill``. Verify the import works."""
        from work_buddy.recovery import recheck_disabled_skill  # noqa: F401
        assert callable(recheck_disabled_skill)

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
        assert "recheck_disabled_skill" in source, (
            "gateway.py no longer references recheck_disabled_skill — "
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
    against actual Skill objects (not just synthetic mocks)."""

    @pytest.fixture
    def fresh_registry(self):
        """Force a registry rebuild with a genuinely-missing dep unavailable.

        Uses ``hindsight`` (a real optional dependency with several single-dep
        skills) as the example missing tool. Obsidian is deliberately
        NOT used here: a missing Obsidian bridge no longer hard-disables its
        dependent skills — that is governed at runtime by the gateway's
        circuit breaker, not the build-time filter (see
        ``test_obsidian_caps_stay_admitted_when_bridge_unavailable``). The
        CP-A1/A2/A3 build-time-disable + lazy-recovery chain this class
        exercises still applies to every genuinely-absent dependency.
        """
        from work_buddy.mcp_server import registry as reg_mod
        from work_buddy.tools import DISABLED_SKILLS

        reg_mod._REGISTRY = None
        reg_mod._DISABLED_SKILL_REGISTRY.clear()
        DISABLED_SKILLS.clear()

        # Build with hindsight unavailable (a genuinely-absent dependency).
        with patch("work_buddy.tools.is_tool_available") as mock_avail:
            mock_avail.side_effect = lambda t: t != "hindsight"
            reg_mod.get_registry()

        yield reg_mod

        # Clean up — force a fresh build on next access.
        reg_mod._REGISTRY = None
        reg_mod._DISABLED_SKILL_REGISTRY.clear()
        DISABLED_SKILLS.clear()

    def test_recheck_restores_real_disabled_skill(self, fresh_registry):
        """Pick a real hindsight-requiring skill from the live
        DISABLED_SKILLS, simulate the dep recovering, and verify
        recheck_disabled_skill restores it to _REGISTRY.

        Co-migrated from an obsidian example: obsidian caps are no longer
        build-time disabled, so this exercises the (dep-agnostic) recovery
        primitive against a genuinely-absent dependency instead.
        """
        from work_buddy.recovery import recheck_disabled_skill
        from work_buddy.tools import DISABLED_SKILLS

        # Find a real hindsight-disabled skill (the registry has several).
        candidates = [
            n for n, deps in DISABLED_SKILLS.items()
            if "hindsight" in deps and len(deps) == 1
        ]
        assert candidates, (
            "Registry has no single-hindsight-dep skills — test "
            "needs a different skill to exercise."
        )
        cap_name = candidates[0]

        # Simulate hindsight recovering.
        with patch("work_buddy.tools.reprobe_one"), \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            recovered = recheck_disabled_skill(cap_name)

        assert recovered is True, (
            f"recheck_disabled_skill returned False for {cap_name}; "
            f"DISABLED_SKILLS={DISABLED_SKILLS.get(cap_name)}"
        )
        assert cap_name in fresh_registry.get_registry(), (
            f"{cap_name} not in live registry post-restore"
        )
        assert cap_name not in DISABLED_SKILLS, (
            f"{cap_name} still in DISABLED_SKILLS post-restore"
        )

    def test_failed_recheck_keeps_skill_disabled(self, fresh_registry):
        """If the probe says still-unavailable, the skill stays
        disabled — caller will see the enriched error message."""
        from work_buddy.recovery import recheck_disabled_skill
        from work_buddy.tools import DISABLED_SKILLS

        candidates = [
            n for n, deps in DISABLED_SKILLS.items()
            if "hindsight" in deps and len(deps) == 1
        ]
        assert candidates
        cap_name = candidates[0]

        with patch("work_buddy.tools.reprobe_one"), \
             patch("work_buddy.tools.is_tool_available", return_value=False):
            recovered = recheck_disabled_skill(cap_name)

        assert recovered is False
        assert cap_name in DISABLED_SKILLS
        assert cap_name not in fresh_registry.get_registry()

    def test_obsidian_caps_stay_admitted_when_bridge_unavailable(self):
        """A missing Obsidian bridge does NOT build-time disable its
        dependent skills. They stay in the live registry (governed at
        runtime by the gateway's circuit breaker) rather than vanishing from
        it for the whole session on a single probe failure."""
        from work_buddy.mcp_server import registry as reg_mod
        from work_buddy.tools import DISABLED_SKILLS

        reg_mod._REGISTRY = None
        reg_mod._DISABLED_SKILL_REGISTRY.clear()
        DISABLED_SKILLS.clear()
        try:
            with patch("work_buddy.tools.is_tool_available") as mock_avail:
                mock_avail.side_effect = lambda t: t != "obsidian"
                reg = reg_mod.get_registry()

            # No skill was disabled SOLELY because obsidian was missing.
            obsidian_only_disabled = [
                n for n, deps in DISABLED_SKILLS.items() if deps == ["obsidian"]
            ]
            assert not obsidian_only_disabled, (
                "obsidian-only skills were build-time disabled; the "
                f"runtime breaker should govern them instead: {obsidian_only_disabled}"
            )
            # And a known obsidian-requiring skill is still callable.
            obsidian_caps_live = [
                n for n, e in reg.items()
                if "obsidian" in (getattr(e, "requires", None) or [])
            ]
            assert obsidian_caps_live, (
                "expected obsidian-requiring skills to remain in the "
                "live registry when the bridge probes unavailable"
            )
        finally:
            reg_mod._REGISTRY = None
            reg_mod._DISABLED_SKILL_REGISTRY.clear()
            DISABLED_SKILLS.clear()
