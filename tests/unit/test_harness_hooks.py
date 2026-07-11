"""Lifecycle hook normalization shared by Claude Code and Codex."""

from __future__ import annotations

import pytest

from work_buddy.harness import hooks


@pytest.fixture(autouse=True)
def isolated_hook_side_effects(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        hooks,
        "_record_session",
        lambda harness_id, session_id, payload, cwd: recorded.append(
            (harness_id, session_id, cwd)
        ),
    )
    monkeypatch.setattr(hooks, "_project_name", lambda cwd: "work-buddy")
    monkeypatch.setattr(hooks, "_recently_checked", lambda session_id: False)
    monkeypatch.setattr(hooks, "_pending_context", lambda **kwargs: "")
    return recorded


@pytest.mark.parametrize(
    ("harness_id", "session_id"),
    [
        ("claudecode", "claude-session"),
        ("codexcli", "codex-thread"),
    ],
)
def test_session_start_surfaces_native_identity(
    harness_id, session_id, isolated_hook_side_effects
):
    result = hooks.handle_hook(
        "session-start",
        harness_id=harness_id,
        payload={"session_id": session_id, "cwd": "C:/repos/work-buddy"},
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert f"session identity: {session_id}" in context
    assert f'harness_id="{harness_id}"' in context
    assert isolated_hook_side_effects == [
        (harness_id, session_id, "C:/repos/work-buddy")
    ]


def test_stop_blocks_only_when_messages_are_pending(monkeypatch):
    monkeypatch.setattr(
        hooks,
        "_pending_context",
        lambda **kwargs: "A teammate left a message.",
    )
    result = hooks.handle_hook(
        "stop",
        harness_id="codexcli",
        payload={"session_id": "codex-thread", "cwd": "C:/repo"},
    )

    assert result["decision"] == "block"
    assert "A teammate left a message." in result["hookSpecificOutput"][
        "additionalContext"
    ]


def test_missing_session_id_returns_actionable_context(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    result = hooks.handle_hook(
        "session-start",
        harness_id="codexcli",
        payload={"cwd": "C:/repo"},
    )
    assert "CODEX_THREAD_ID" in result["hookSpecificOutput"]["additionalContext"]


def test_parse_hook_payload_rejects_non_object_json():
    with pytest.raises(ValueError, match="JSON object"):
        hooks.parse_hook_payload("[]")
