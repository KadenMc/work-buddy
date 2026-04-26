"""CP-A4: tests for the conductor's lazy auto-recovery on workflow steps.

When a workflow step's ``requires`` list names a tool that's currently
reporting unavailable, the conductor calls ``recheck_tool`` to re-probe
before failing the step. Mirrors the gateway's CP-A3 wiring for the
direct-capability dispatch path.

These tests focus on the recheck-decision logic without spinning up a
full workflow DAG. The advance loop's surface is too broad to mock
end-to-end here; we verify the BEHAVIOR of the recheck wiring through
the recovery primitives (which are exhaustively tested in
``test_recovery.py``) plus a source-reference smoke check.
"""
from __future__ import annotations

import importlib

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Wiring smoke: conductor source references the recovery primitive
# ---------------------------------------------------------------------------


class TestConductorWiringSmoke:
    def test_conductor_imports_recheck_tool(self):
        """Sanity: the recovery primitive is importable from where the
        conductor's CP-A4 path imports it."""
        from work_buddy.recovery import recheck_tool  # noqa: F401
        assert callable(recheck_tool)

    def test_conductor_module_loads(self):
        conductor = importlib.import_module("work_buddy.mcp_server.conductor")
        assert hasattr(conductor, "advance_workflow")

    def test_conductor_source_references_recheck(self):
        """Catches accidental rip-out of the CP-A4 recheck wiring."""
        from pathlib import Path

        conductor_path = (
            Path(__file__).parent.parent.parent
            / "work_buddy" / "mcp_server" / "conductor.py"
        )
        source = conductor_path.read_text(encoding="utf-8")
        assert "recheck_tool" in source, (
            "conductor.py no longer references recheck_tool — the CP-A4 "
            "auto-recovery wiring may have been removed."
        )
        # Conditional gating: only re-probe tools that ARE currently
        # reporting unavailable (don't waste cycles on healthy tools).
        assert "if not is_tool_available(t):" in source, (
            "conductor.py no longer guards recheck_tool behind an "
            "is_tool_available check — recheck would fire on every "
            "step regardless of need, breaking the per-tool cool-down "
            "design."
        )


# ---------------------------------------------------------------------------
# Behavior: recheck_tool wiring fires only for unavailable tools
# ---------------------------------------------------------------------------


class TestRecheckOnlyForUnavailable:
    """The conductor wiring only calls ``recheck_tool`` for tools that
    are currently reporting unavailable. This avoids paying probe cost
    on healthy paths."""

    def _simulate_conductor_check(self, step_requires: list[str], available_set: set[str]) -> tuple[list[str], list[str]]:
        """Run the same recheck-then-test loop the conductor uses.

        Returns (missing_tools, recheck_calls). The available_set is
        what is_tool_available returns; we don't simulate a recheck
        flipping the state mid-call (the recovery primitive's tests
        cover that).
        """
        from work_buddy.recovery import recheck_tool

        recheck_calls: list[str] = []

        def fake_avail(tool_id):
            return tool_id in available_set

        def fake_recheck(tool_id, *, force=False):
            recheck_calls.append(tool_id)
            # Don't actually probe — the gating logic is what we're testing.
            return tool_id in available_set

        with patch("work_buddy.tools.is_tool_available", side_effect=fake_avail), \
             patch("work_buddy.recovery.recheck_tool", side_effect=fake_recheck) as mock_recheck:
            # Replicate the conductor's CP-A4 loop body.
            from work_buddy.tools import is_tool_available
            from work_buddy.recovery import recheck_tool as rt  # picks up the patch

            missing_tools: list[str] = []
            for t in step_requires:
                if not is_tool_available(t):
                    rt(t)
                    if not is_tool_available(t):
                        missing_tools.append(t)

        return missing_tools, recheck_calls

    def test_no_recheck_when_all_tools_healthy(self):
        """All tools available → recheck_tool never called."""
        missing, recheck_calls = self._simulate_conductor_check(
            step_requires=["obsidian", "messaging"],
            available_set={"obsidian", "messaging"},
        )
        assert missing == []
        assert recheck_calls == []

    def test_recheck_called_only_for_unavailable_tools(self):
        """Mix of available + unavailable: recheck only for the
        unavailable ones."""
        missing, recheck_calls = self._simulate_conductor_check(
            step_requires=["obsidian", "messaging", "chrome_extension"],
            available_set={"obsidian"},  # only obsidian is up
        )
        # messaging + chrome are still unavailable post-recheck.
        assert sorted(missing) == ["chrome_extension", "messaging"]
        # recheck was called for the two unavailable, NOT for obsidian.
        assert sorted(recheck_calls) == ["chrome_extension", "messaging"]
        assert "obsidian" not in recheck_calls

    def test_all_tools_unavailable_all_get_rechecked(self):
        missing, recheck_calls = self._simulate_conductor_check(
            step_requires=["obsidian", "messaging"],
            available_set=set(),  # nothing available
        )
        assert sorted(missing) == ["messaging", "obsidian"]
        assert sorted(recheck_calls) == ["messaging", "obsidian"]


# ---------------------------------------------------------------------------
# End-to-end: recheck flips a tool from unavailable→available
# ---------------------------------------------------------------------------


class TestRecheckRecovers:
    """Exercises the real ``recheck_tool`` against simulated probe state
    transitions. If the probe says "now available" on the second call,
    the conductor's loop sees it and the step proceeds."""

    @pytest.fixture(autouse=True)
    def reset_recovery_state(self):
        from work_buddy import recovery
        recovery._LAST_RECHECK_AT.clear()
        recovery.set_cooldown_seconds(30.0)
        yield
        recovery._LAST_RECHECK_AT.clear()
        recovery.set_cooldown_seconds(30.0)

    def test_step_proceeds_when_recheck_recovers(self):
        """is_tool_available initially False, recheck triggers a probe,
        post-probe state flips to True → loop produces no missing_tools.

        Note: we import is_tool_available INSIDE the patch block so we
        pick up the patched function (top-level imports cache the real
        function reference before the patch applies)."""
        from work_buddy.recovery import recheck_tool

        # Sequence: pre-recheck=False, post-probe=True.
        avail_state = {"obsidian": False}

        def fake_avail(tool_id):
            return avail_state.get(tool_id, False)

        def fake_reprobe(tool_id):
            # The probe magically discovers the tool is up now.
            avail_state[tool_id] = True

        with patch("work_buddy.tools.is_tool_available", side_effect=fake_avail), \
             patch("work_buddy.tools.reprobe_one", side_effect=fake_reprobe):
            # Re-import inside the patch block so we get the patched
            # version (mirrors how the conductor lazy-imports it).
            from work_buddy.tools import is_tool_available

            # Conductor's loop body, faithfully reproduced.
            step_requires = ["obsidian"]
            missing_tools: list[str] = []
            for t in step_requires:
                if not is_tool_available(t):
                    recheck_tool(t)
                    if not is_tool_available(t):
                        missing_tools.append(t)

        assert missing_tools == [], (
            "recheck_tool should have flipped obsidian to available; "
            "step should have no missing tools"
        )
        assert avail_state["obsidian"] is True

    def test_step_fails_when_recheck_does_not_recover(self):
        """If recheck runs but the probe still says unavailable, the
        step's missing_tools list includes the tool — conductor will
        fail/skip per existing logic."""
        from work_buddy.recovery import recheck_tool

        with patch("work_buddy.tools.is_tool_available", return_value=False), \
             patch("work_buddy.tools.reprobe_one") as mock_probe:
            from work_buddy.tools import is_tool_available

            step_requires = ["obsidian"]
            missing_tools: list[str] = []
            for t in step_requires:
                if not is_tool_available(t):
                    recheck_tool(t)
                    if not is_tool_available(t):
                        missing_tools.append(t)

        assert missing_tools == ["obsidian"]
        # Probe was actually called (recheck wiring works).
        assert mock_probe.call_count == 1

    def test_repeated_workflow_steps_share_cooldown(self):
        """A workflow with N steps all requiring the same tool should
        only probe that tool ONCE (subsequent recheck calls hit the
        cool-down and skip)."""
        from work_buddy.recovery import recheck_tool

        with patch("work_buddy.tools.is_tool_available", return_value=False), \
             patch("work_buddy.tools.reprobe_one") as mock_probe:
            from work_buddy.tools import is_tool_available

            # Five steps, each requiring obsidian.
            for _ in range(5):
                step_requires = ["obsidian"]
                for t in step_requires:
                    if not is_tool_available(t):
                        recheck_tool(t)

        assert mock_probe.call_count == 1, (
            f"Expected 1 probe across 5 same-tool step rechecks "
            f"(cool-down should suppress repeats), got {mock_probe.call_count}"
        )
