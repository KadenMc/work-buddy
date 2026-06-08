"""Tests for the disambiguation of ``disabled`` vs ``unavailable`` on
wb_search results, and for the workflow-level parity in flagging.

Context: During a retro (2026-04-17) a reasoning model saw
``unavailable: true`` on a capability search result and concluded
"I don't have permission to use this" — but the flag actually meant
"the backing service is down (Obsidian bridge probed offline)". The
two failure modes had been collapsed into one ambiguous field.

Also fixed: workflows in the knowledge store but not registered
(tool deps unmet) used to come back WITHOUT any disabled flag at
all — agents would see a clean hit and try to run a workflow whose
dependencies weren't met. Now workflow hits mirror the capability
branch: ``disabled: true`` + a ``disabled_reason`` explaining why.
"""

from __future__ import annotations

from unittest.mock import patch

from work_buddy.mcp_server.registry import _disabled_reason


def test_disabled_reason_surfaces_missing_deps():
    """When DISABLED_CAPABILITIES has an entry, the reason names it."""
    with patch(
        "work_buddy.tools.DISABLED_CAPABILITIES",
        {"journal_write": ["obsidian"]},
    ):
        reason = _disabled_reason("journal_write")
    assert "obsidian" in reason
    assert "Dependency unavailable" in reason


def test_disabled_reason_handles_multiple_deps():
    with patch(
        "work_buddy.tools.DISABLED_CAPABILITIES",
        {"datacore_query": ["obsidian", "datacore"]},
    ):
        reason = _disabled_reason("datacore_query")
    assert "obsidian" in reason
    assert "datacore" in reason


# ---------------------------------------------------------------------------
# CP-A5: enriched _disabled_reason with fresh probe state
# ---------------------------------------------------------------------------


class TestDisabledReasonEnriched:
    """Post-CP-A5 the reason string is per-tool and includes the probe's
    current state + why-it-failed + how-long-ago. Three states per tool:
    probe-still-failing, probe-now-passing-but-stale-registry, no-probe-data.
    """

    def test_probe_still_failing_includes_reason_and_age(self):
        with patch(
            "work_buddy.tools.DISABLED_CAPABILITIES",
            {"journal_write": ["obsidian"]},
        ), patch(
            "work_buddy.tools.get_tool_status",
            return_value={
                "tools": {
                    "obsidian": {
                        "available": False,
                        "probe_ms": 530.5,
                        "reason": "Bridge unreachable",
                        "config_enabled": True,
                    },
                },
            },
        ):
            reason = _disabled_reason("journal_write")

        assert "obsidian" in reason
        assert "probe failed" in reason
        assert "Bridge unreachable" in reason
        assert "ago" in reason  # probe age formatting

    def test_probe_now_passing_recommends_reload_capability_data(self):
        """The rare race where the probe is reporting available but the
        capability is still in DISABLED_CAPABILITIES — should suggest
        the manual remediation."""
        with patch(
            "work_buddy.tools.DISABLED_CAPABILITIES",
            {"journal_write": ["obsidian"]},
        ), patch(
            "work_buddy.tools.get_tool_status",
            return_value={
                "tools": {
                    "obsidian": {
                        "available": True,
                        "probe_ms": 42.0,
                        "reason": "",
                        "config_enabled": True,
                    },
                },
            },
        ):
            reason = _disabled_reason("journal_write")

        assert "available" in reason.lower()
        assert "reload_capability_data" in reason

    def test_no_probe_data_yet_distinct_message(self):
        """Cold-start race: tool isn't in get_tool_status's tools dict
        at all (probe hasn't completed yet)."""
        with patch(
            "work_buddy.tools.DISABLED_CAPABILITIES",
            {"journal_write": ["obsidian"]},
        ), patch(
            "work_buddy.tools.get_tool_status",
            return_value={"tools": {}},
        ):
            reason = _disabled_reason("journal_write")

        assert "obsidian" in reason
        assert "no probe data yet" in reason

    def test_multi_tool_reports_each_state_separately(self):
        """Multi-tool capability: one tool failing, one tool passing-but-
        stale. Each reported with its own state."""
        with patch(
            "work_buddy.tools.DISABLED_CAPABILITIES",
            {"datacore_query": ["obsidian", "datacore"]},
        ), patch(
            "work_buddy.tools.get_tool_status",
            return_value={
                "tools": {
                    "obsidian": {
                        "available": False,
                        "probe_ms": 530.0,
                        "reason": "Bridge unreachable",
                        "config_enabled": True,
                    },
                    "datacore": {
                        "available": True,
                        "probe_ms": 100.0,
                        "reason": "",
                        "config_enabled": True,
                    },
                },
            },
        ):
            reason = _disabled_reason("datacore_query")

        assert "obsidian" in reason
        assert "Bridge unreachable" in reason
        assert "datacore" in reason
        assert "available" in reason.lower()
        assert "reload_capability_data" in reason

    def test_falls_back_when_get_tool_status_raises(self):
        """If the enriched-message machinery throws, we still return the
        legacy 'Dependency unavailable: <deps>' string instead of crashing."""
        with patch(
            "work_buddy.tools.DISABLED_CAPABILITIES",
            {"journal_write": ["obsidian"]},
        ), patch(
            "work_buddy.tools.get_tool_status",
            side_effect=RuntimeError("simulated failure"),
        ):
            reason = _disabled_reason("journal_write")

        assert isinstance(reason, str)
        assert reason
        assert "obsidian" in reason


class TestProbeAgeFormat:
    """The _format_probe_age helper is a pure formatter on the
    tool_status.json file mtime. Tests cover the three time bands."""

    def test_seconds_formatting(self, tmp_path, monkeypatch):
        from work_buddy.mcp_server.registry import _format_probe_age
        import time

        f = tmp_path / "tool_status.json"
        f.write_text("{}")
        # Mtime is "now" — should be near-zero seconds ago.
        monkeypatch.setattr(
            "work_buddy.tools._TOOL_STATUS_FILE", f,
        )
        out = _format_probe_age()
        assert out.endswith("s ago"), f"expected seconds-band, got {out!r}"

    def test_minutes_formatting(self, tmp_path, monkeypatch):
        from work_buddy.mcp_server.registry import _format_probe_age
        import os
        import time

        f = tmp_path / "tool_status.json"
        f.write_text("{}")
        # Set mtime to 5 minutes ago.
        five_min_ago = time.time() - 300
        os.utime(f, (five_min_ago, five_min_ago))
        monkeypatch.setattr(
            "work_buddy.tools._TOOL_STATUS_FILE", f,
        )
        out = _format_probe_age()
        assert out.endswith("m ago"), f"expected minutes-band, got {out!r}"

    def test_unknown_when_file_missing(self, tmp_path, monkeypatch):
        from work_buddy.mcp_server.registry import _format_probe_age

        f = tmp_path / "does_not_exist.json"
        monkeypatch.setattr(
            "work_buddy.tools._TOOL_STATUS_FILE", f,
        )
        out = _format_probe_age()
        assert "unknown" in out


def test_disabled_reason_fallback_for_unknown_capability():
    """Not in DISABLED_CAPABILITIES (genuinely not in the live
    registry, for reasons other than unmet deps). Reason still
    readable and non-empty."""
    with patch("work_buddy.tools.DISABLED_CAPABILITIES", {}):
        reason = _disabled_reason("something_not_even_a_capability")
    assert reason
    assert isinstance(reason, str)
    # Sentinel phrase from the helper's fallback path
    assert "Not registered" in reason


def test_disabled_reason_survives_import_failure(monkeypatch):
    """If DISABLED_CAPABILITIES isn't importable (shouldn't happen in
    practice, but guarded in the helper), we still get a usable
    string instead of an exception propagating into wb_search."""
    import work_buddy.mcp_server.registry as registry

    # Simulate the import failing by patching the whole tools module
    # access — the helper catches Exception broadly
    def _raising_import(*args, **kwargs):
        raise RuntimeError("simulated import failure")

    monkeypatch.setattr(
        "work_buddy.tools.DISABLED_CAPABILITIES",
        property(_raising_import),
    )
    # The helper should still return a string, not raise
    result = registry._disabled_reason("anything")
    assert isinstance(result, str)
    assert result
