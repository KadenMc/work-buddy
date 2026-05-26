"""Unit tests — gateway pre-flight helpers for workflow consent.

Exercise the helpers in ``work_buddy/mcp_server/tools/gateway.py`` that
back the workflow-consent pre-flight prompt at workflow start. The full
dispatcher path (notification delivery → poll → response) is exercised
by the integration suite; here we cover the building blocks:

- ``_is_workflow_class_authorized`` — checks whether the workflow has a
  live ``workflow_class:<name>`` grant in the session.
- ``_collect_workflow_consent_ops`` — walks a workflow's DAG and returns
  the union of declared ``consent_operations`` plus the max risk.
- ``_render_workflow_consent_body`` — formats the modal body string.
- ``_auto_workflow_consent_request`` early-return paths:
    * Low-weight workflows auto-bypass with status
      ``"auto_bypass_low_weight"`` and an audit-log entry.
- ``user_initiated()`` bypass — when the context is active, the gateway's
  workflow dispatch skips the pre-flight entirely. Verified here via the
  ``_consent_ctx.depth`` primitive that the gateway reads.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cache(tmp_agents_dir, monkeypatch):
    """Shared fixture identical to test_consent_composable.py — reset
    consent state and restore the canonical ``get_agents_dir`` so this
    suite is isolated from earlier files' setUps."""
    import work_buddy.agent_session as asmod
    def _canonical_get_agents_dir():
        return asmod.data_dir("agents")
    monkeypatch.setattr(asmod, "get_agents_dir", _canonical_get_agents_dir)
    monkeypatch.setattr(asmod, "_cached_session_dir", None)

    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    from work_buddy import consent as cmod
    cmod._LEGACY_BLANKET_LOGGED.clear()
    return _cache


# ---------------------------------------------------------------------------
# _is_workflow_class_authorized
# ---------------------------------------------------------------------------


def test_is_workflow_class_authorized_returns_false_without_grant(cache):
    """No class grant → not authorized."""
    from work_buddy.mcp_server.tools.gateway import (
        _is_workflow_class_authorized,
    )
    assert _is_workflow_class_authorized(
        "task-new", session_id=None,
    ) is False


def test_is_workflow_class_authorized_returns_true_with_grant(cache):
    """Granting workflow_class makes the helper return True."""
    from work_buddy.consent import grant_workflow_class
    from work_buddy.mcp_server.tools.gateway import (
        _is_workflow_class_authorized,
    )
    grant_workflow_class("task-new", ttl_minutes=15)
    assert _is_workflow_class_authorized(
        "task-new", session_id=None,
    ) is True


def test_is_workflow_class_authorized_is_per_workflow(cache):
    """A class grant for one workflow does not authorize another."""
    from work_buddy.consent import grant_workflow_class
    from work_buddy.mcp_server.tools.gateway import (
        _is_workflow_class_authorized,
    )
    grant_workflow_class("task-new", ttl_minutes=15)
    assert _is_workflow_class_authorized("morning-routine", session_id=None) is False


# ---------------------------------------------------------------------------
# _collect_workflow_consent_ops
# ---------------------------------------------------------------------------


def _make_workflow_def(steps_invokes: list[list[str]]):
    """Helper: build a minimal WorkflowDefinition with the given
    per-step ``invokes`` lists."""
    from work_buddy.mcp_server.registry import (
        WorkflowDefinition, WorkflowStep,
    )
    steps = [
        WorkflowStep(
            id=f"s{i}",
            name=f"Step {i}",
            instruction="",
            step_type="code",
            invokes=invokes,
        )
        for i, invokes in enumerate(steps_invokes)
    ]
    return WorkflowDefinition(
        name="test-wf",
        description="A test workflow.",
        workflow_file="",
        execution="main",
        steps=steps,
    )


def test_collect_workflow_consent_ops_empty_when_no_invokes(cache):
    """Workflow with no invokes → no consent ops."""
    from work_buddy.mcp_server.tools.gateway import _collect_workflow_consent_ops
    entry = _make_workflow_def([[]])
    ops, max_risk = _collect_workflow_consent_ops(entry)
    assert ops == []
    assert max_risk == "low"


def test_collect_workflow_consent_ops_deduplicates(cache):
    """Same capability invoked from multiple steps contributes once."""
    from work_buddy.mcp_server.tools.gateway import _collect_workflow_consent_ops
    entry = _make_workflow_def([["cap_a"], ["cap_a", "cap_b"]])
    # No real capabilities registered for cap_a / cap_b → empty result.
    # The test verifies the function handles unknown caps without raising.
    ops, max_risk = _collect_workflow_consent_ops(entry)
    assert ops == []
    assert max_risk == "low"


# ---------------------------------------------------------------------------
# _render_workflow_consent_body
# ---------------------------------------------------------------------------


def test_render_workflow_consent_body_includes_workflow_name_and_description(cache):
    from work_buddy.mcp_server.tools.gateway import (
        _render_workflow_consent_body,
    )
    entry = _make_workflow_def([[]])
    body = _render_workflow_consent_body("task-new", entry, [])
    assert "task-new" in body
    assert "A test workflow." in body


def test_render_workflow_consent_body_lists_consent_ops(cache):
    from work_buddy.mcp_server.tools.gateway import (
        _render_workflow_consent_body,
    )
    entry = _make_workflow_def([[]])
    body = _render_workflow_consent_body(
        "task-new", entry, ["tasks.create_task", "obsidian.write_file"],
    )
    assert "tasks.create_task" in body
    assert "obsidian.write_file" in body


def test_render_workflow_consent_body_notes_no_consent_ops(cache):
    from work_buddy.mcp_server.tools.gateway import (
        _render_workflow_consent_body,
    )
    entry = _make_workflow_def([[]])
    body = _render_workflow_consent_body("noop-wf", entry, [])
    assert "No consent-gated operations" in body


# ---------------------------------------------------------------------------
# _auto_workflow_consent_request — early-return paths
# ---------------------------------------------------------------------------


def test_low_weight_workflow_auto_bypasses_prompt(cache):
    """A workflow with no declared consent ops (or only low-risk ones)
    auto-bypasses the prompt. The function returns
    status='auto_bypass_low_weight' without contacting the notification
    system."""
    from work_buddy.mcp_server.tools.gateway import (
        _auto_workflow_consent_request,
    )
    entry = _make_workflow_def([[]])
    result = _auto_workflow_consent_request(
        "low-weight-wf", entry, "op_test", session_id=None,
    )
    assert result["status"] == "auto_bypass_low_weight"
    assert result["workflow_name"] == "low-weight-wf"


def test_auto_bypass_writes_audit_log(cache, monkeypatch):
    """The auto-bypass writes a WORKFLOW_AUTO_BYPASS_LOW_WEIGHT audit
    line so the audit script can find unconverted workflows."""
    from work_buddy import consent as cmod
    from work_buddy.mcp_server.tools.gateway import (
        _auto_workflow_consent_request,
    )

    captured: list[tuple[str, str, str]] = []
    def _capture(event, operation, details=""):
        captured.append((event, operation, details))
    monkeypatch.setattr(cmod, "_audit_log", _capture)

    entry = _make_workflow_def([[]])
    _auto_workflow_consent_request(
        "low-weight-wf", entry, "op_test", session_id=None,
    )

    bypass_events = [
        e for (e, op, _) in captured
        if e == "WORKFLOW_AUTO_BYPASS_LOW_WEIGHT" and op == "low-weight-wf"
    ]
    assert bypass_events, "expected WORKFLOW_AUTO_BYPASS_LOW_WEIGHT audit entry"


# ---------------------------------------------------------------------------
# Slash-command / user_initiated bypass primitive
# ---------------------------------------------------------------------------


def test_user_initiated_increments_consent_depth(cache):
    """``user_initiated()`` sets ``_consent_ctx.depth > 0``; the gateway's
    workflow dispatch reads this flag to skip the pre-flight prompt
    entirely. Slash-command launchers and other UI-driven callers wrap
    their dispatch in ``user_initiated()`` to opt into the bypass.
    """
    from work_buddy.consent import user_initiated, _consent_ctx

    assert _consent_ctx.depth == 0
    with user_initiated("test.bypass"):
        assert _consent_ctx.depth == 1
        # Nested re-entry is supported.
        with user_initiated("test.bypass.inner"):
            assert _consent_ctx.depth == 2
        assert _consent_ctx.depth == 1
    assert _consent_ctx.depth == 0
