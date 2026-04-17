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
        {"context_smart": ["obsidian", "smart_connections"]},
    ):
        reason = _disabled_reason("context_smart")
    assert "obsidian" in reason
    assert "smart_connections" in reason


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
