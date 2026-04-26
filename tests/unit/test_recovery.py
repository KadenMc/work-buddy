"""Unit tests for the lazy auto-recovery primitives.

Covers:
- ``recheck_tool``: cool-down honoured, force bypasses cool-down, probe
  failures don't update timestamp (so next call retries cleanly).
- ``recheck_disabled_capability``: full recovery (all tools available),
  partial recovery (some tools still missing — DISABLED_CAPABILITIES
  shrinks but cap stays out of registry), unknown name returns True
  without probing, restore-order safety (_REGISTRY populated before
  disabled maps cleared).
- Concurrency: parallel callers serialize through _RECOVERY_LOCK and
  share the result of a single probe.
- ``reload_registry_under_lock``: clears _LAST_RECHECK_AT; cooperates
  with concurrent rechecks.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_recovery_state():
    """Reset module-level state between tests."""
    from work_buddy import recovery
    from work_buddy.tools import DISABLED_CAPABILITIES
    from work_buddy.mcp_server.registry import _DISABLED_REGISTRY, _REGISTRY  # noqa: F401

    recovery._LAST_RECHECK_AT.clear()
    recovery.set_cooldown_seconds(30.0)
    DISABLED_CAPABILITIES.clear()
    _DISABLED_REGISTRY.clear()
    yield
    recovery._LAST_RECHECK_AT.clear()
    recovery.set_cooldown_seconds(30.0)
    DISABLED_CAPABILITIES.clear()
    _DISABLED_REGISTRY.clear()


# ---------------------------------------------------------------------------
# recheck_tool
# ---------------------------------------------------------------------------


class TestRecheckTool:
    def test_returns_fresh_status_after_probe(self):
        """First call probes the tool and returns is_tool_available's result."""
        from work_buddy import recovery

        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            result = recovery.recheck_tool("obsidian")

        assert result is True
        mock_probe.assert_called_once_with("obsidian")

    def test_returns_false_when_probe_says_unavailable(self):
        from work_buddy import recovery

        with patch("work_buddy.tools.reprobe_one"), \
             patch("work_buddy.tools.is_tool_available", return_value=False):
            assert recovery.recheck_tool("obsidian") is False

    def test_respects_cooldown(self):
        """Two calls within the cool-down window → only one probe."""
        from work_buddy import recovery

        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            recovery.recheck_tool("obsidian")
            recovery.recheck_tool("obsidian")

        assert mock_probe.call_count == 1

    def test_force_bypasses_cooldown(self):
        """force=True re-probes even within the cool-down window."""
        from work_buddy import recovery

        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            recovery.recheck_tool("obsidian")
            recovery.recheck_tool("obsidian", force=True)

        assert mock_probe.call_count == 2

    def test_cooldown_per_tool(self):
        """Cool-down is per-tool — re-probing a different tool isn't blocked."""
        from work_buddy import recovery

        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            recovery.recheck_tool("obsidian")
            recovery.recheck_tool("chrome_extension")

        assert mock_probe.call_count == 2
        called_tools = {c.args[0] for c in mock_probe.call_args_list}
        assert called_tools == {"obsidian", "chrome_extension"}

    def test_probe_failure_does_not_update_timestamp(self):
        """If reprobe_one raises, _LAST_RECHECK_AT shouldn't advance,
        so the next call retries instead of waiting out the cool-down."""
        from work_buddy import recovery

        # First call: probe raises.
        with patch("work_buddy.tools.reprobe_one", side_effect=RuntimeError("boom")), \
             patch("work_buddy.tools.is_tool_available", return_value=False):
            recovery.recheck_tool("obsidian")

        assert "obsidian" not in recovery._LAST_RECHECK_AT, (
            "Failed probe must not advance the cool-down timestamp"
        )

        # Second call: probe should run again (no cool-down hit).
        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            recovery.recheck_tool("obsidian")
            assert mock_probe.call_count == 1

    def test_zero_cooldown_always_reprobes(self):
        from work_buddy import recovery

        recovery.set_cooldown_seconds(0.0)
        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            recovery.recheck_tool("obsidian")
            recovery.recheck_tool("obsidian")

        # cool-down=0 means elapsed > 0 always passes the gate.
        assert mock_probe.call_count == 2


# ---------------------------------------------------------------------------
# recheck_disabled_capability
# ---------------------------------------------------------------------------


def _make_capability(name: str, requires: list[str]):
    """Build a minimal Capability instance for tests."""
    from work_buddy.mcp_server.registry import Capability

    return Capability(
        name=name,
        description=f"test capability {name}",
        category="test",
        parameters={},
        callable=lambda: {"test": name},
        requires=requires,
    )


def _seed_disabled(name: str, missing: list[str], capability=None):
    """Populate DISABLED_CAPABILITIES + _DISABLED_REGISTRY for one cap."""
    from work_buddy.tools import DISABLED_CAPABILITIES
    from work_buddy.mcp_server.registry import _DISABLED_REGISTRY

    DISABLED_CAPABILITIES[name] = list(missing)
    _DISABLED_REGISTRY[name] = capability or _make_capability(name, missing)


class TestRecheckDisabledCapability:
    def test_unknown_name_returns_true_without_probing(self):
        """Capability not in DISABLED_CAPABILITIES → no-op, returns True."""
        from work_buddy import recovery

        with patch("work_buddy.tools.reprobe_one") as mock_probe:
            result = recovery.recheck_disabled_capability("never_disabled")

        assert result is True
        assert mock_probe.call_count == 0

    def test_all_tools_recovered_restores_capability(self):
        """All missing tools probe as available → cap restored to live registry."""
        from work_buddy import recovery
        from work_buddy.mcp_server.registry import (
            _DISABLED_REGISTRY,
            get_registry,
        )
        from work_buddy.tools import DISABLED_CAPABILITIES

        cap = _make_capability("test_cap", ["obsidian"])
        _seed_disabled("test_cap", ["obsidian"], cap)
        # Ensure registry is initialised so the restore can land somewhere.
        with patch("work_buddy.tools.is_tool_available", return_value=True):
            registry = get_registry()
            # Re-seed because get_registry may have rebuilt.
            _seed_disabled("test_cap", ["obsidian"], cap)

            with patch("work_buddy.tools.reprobe_one"):
                result = recovery.recheck_disabled_capability("test_cap")

        assert result is True
        assert "test_cap" in registry, "capability should be in live registry post-restore"
        assert "test_cap" not in DISABLED_CAPABILITIES
        assert "test_cap" not in _DISABLED_REGISTRY

    def test_partial_recovery_shrinks_missing_list(self):
        """Multi-tool cap, one tool recovers, one stays down. Cap stays
        disabled but DISABLED_CAPABILITIES[name] shrinks."""
        from work_buddy import recovery
        from work_buddy.tools import DISABLED_CAPABILITIES

        cap = _make_capability("multi_cap", ["obsidian", "chrome_extension"])
        _seed_disabled("multi_cap", ["obsidian", "chrome_extension"], cap)

        # Mock: obsidian recovers (True), chrome stays down (False).
        def fake_avail(tool_id):
            return tool_id == "obsidian"

        with patch("work_buddy.tools.reprobe_one"), \
             patch("work_buddy.tools.is_tool_available", side_effect=fake_avail):
            result = recovery.recheck_disabled_capability("multi_cap")

        assert result is False
        assert DISABLED_CAPABILITIES["multi_cap"] == ["chrome_extension"], (
            "Recovered tools should be removed from the missing list"
        )

    def test_no_tools_recover_keeps_full_missing_list(self):
        from work_buddy import recovery
        from work_buddy.tools import DISABLED_CAPABILITIES

        cap = _make_capability("test_cap", ["obsidian"])
        _seed_disabled("test_cap", ["obsidian"], cap)

        with patch("work_buddy.tools.reprobe_one"), \
             patch("work_buddy.tools.is_tool_available", return_value=False):
            result = recovery.recheck_disabled_capability("test_cap")

        assert result is False
        assert DISABLED_CAPABILITIES["test_cap"] == ["obsidian"]

    def test_cooldown_skipped_for_recently_probed_tools(self):
        """If recheck_tool ran recently for a tool, the disabled-capability
        recheck doesn't re-probe it (uses cached availability)."""
        from work_buddy import recovery

        cap = _make_capability("test_cap", ["obsidian"])
        _seed_disabled("test_cap", ["obsidian"], cap)

        # Pre-warm the cool-down timestamp.
        recovery._LAST_RECHECK_AT["obsidian"] = time.monotonic()

        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=False):
            recovery.recheck_disabled_capability("test_cap")

        assert mock_probe.call_count == 0, (
            "Cool-down should suppress re-probe within the window"
        )

    def test_force_bypasses_cooldown_for_capability_recheck(self):
        from work_buddy import recovery

        cap = _make_capability("test_cap", ["obsidian"])
        _seed_disabled("test_cap", ["obsidian"], cap)
        recovery._LAST_RECHECK_AT["obsidian"] = time.monotonic()

        with patch("work_buddy.tools.reprobe_one") as mock_probe, \
             patch("work_buddy.tools.is_tool_available", return_value=False):
            recovery.recheck_disabled_capability("test_cap", force=True)

        assert mock_probe.call_count == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_recheck_tool_calls_serialize(self):
        """N parallel threads call recheck_tool for the same tool.
        Cool-down + lock should serialize them so reprobe_one runs ONCE."""
        from work_buddy import recovery

        probe_calls = []
        probe_lock = threading.Lock()

        def slow_probe(tool_id):
            # Simulate a slow probe so threads have a real chance to overlap.
            with probe_lock:
                probe_calls.append(tool_id)
            time.sleep(0.05)

        with patch("work_buddy.tools.reprobe_one", side_effect=slow_probe), \
             patch("work_buddy.tools.is_tool_available", return_value=True):
            threads = [
                threading.Thread(target=recovery.recheck_tool, args=("obsidian",))
                for _ in range(10)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(probe_calls) == 1, (
            f"Expected exactly 1 probe call (lock + cool-down should serialize), "
            f"got {len(probe_calls)}"
        )

    def test_concurrent_recheck_disabled_capability_serializes(self):
        """N parallel threads call recheck_disabled_capability. The first
        restores the capability; subsequent callers see early-return."""
        from work_buddy import recovery
        from work_buddy.mcp_server.registry import get_registry

        cap = _make_capability("concurrent_cap", ["obsidian"])
        with patch("work_buddy.tools.is_tool_available", return_value=True):
            get_registry()
            _seed_disabled("concurrent_cap", ["obsidian"], cap)

            probe_calls = []
            probe_lock = threading.Lock()

            def slow_probe(tool_id):
                with probe_lock:
                    probe_calls.append(tool_id)
                time.sleep(0.05)

            with patch("work_buddy.tools.reprobe_one", side_effect=slow_probe):
                threads = [
                    threading.Thread(
                        target=recovery.recheck_disabled_capability,
                        args=("concurrent_cap",),
                    )
                    for _ in range(10)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

        # Only the first caller probes; the rest see the cap already restored
        # and early-return via the "not in DISABLED_CAPABILITIES" guard.
        assert len(probe_calls) == 1, (
            f"Expected exactly 1 probe call, got {len(probe_calls)}"
        )


# ---------------------------------------------------------------------------
# reload_registry_under_lock
# ---------------------------------------------------------------------------


class TestReloadUnderLock:
    def test_reload_clears_recheck_timestamps(self):
        """After reload, _LAST_RECHECK_AT should be empty so post-reload
        probes run fresh (no stale cool-down)."""
        from work_buddy import recovery

        recovery._LAST_RECHECK_AT["obsidian"] = time.monotonic()
        recovery._LAST_RECHECK_AT["chrome_extension"] = time.monotonic()

        with patch("work_buddy.mcp_server.registry.invalidate_registry"):
            recovery.reload_registry_under_lock()

        assert recovery._LAST_RECHECK_AT == {}

    def test_reload_calls_invalidate_registry(self):
        """The wrapper actually invokes invalidate_registry."""
        from work_buddy import recovery

        with patch(
            "work_buddy.mcp_server.registry.invalidate_registry",
        ) as mock_invalidate:
            recovery.reload_registry_under_lock()

        mock_invalidate.assert_called_once()


# ---------------------------------------------------------------------------
# Cool-down configuration
# ---------------------------------------------------------------------------


class TestCooldownConfig:
    def test_set_cooldown_seconds_takes_effect(self):
        from work_buddy import recovery

        recovery.set_cooldown_seconds(5.0)
        assert recovery.get_cooldown_seconds() == 5.0

    def test_set_cooldown_seconds_clamps_negative(self):
        from work_buddy import recovery

        recovery.set_cooldown_seconds(-1.0)
        assert recovery.get_cooldown_seconds() == 0.0
