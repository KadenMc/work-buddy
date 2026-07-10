"""Canonical transcript behavior across first-class harnesses."""

from __future__ import annotations

import json
from pathlib import Path

from work_buddy.transcripts.providers.claude import ClaudeTranscriptProvider
from work_buddy.transcripts.providers.codex import CodexTranscriptProvider


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def test_claude_provider_emits_canonical_turns_and_tool_calls(tmp_path):
    root = tmp_path / ".claude" / "projects"
    session_id = "11111111-2222-3333-4444-555555555555"
    path = _write_jsonl(
        root / "C--repos-work-buddy" / f"{session_id}.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-07-10T10:00:00Z",
                "cwd": "C:/repos/work-buddy",
                "message": {"role": "user", "content": "Ship it"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-10T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Running tests."},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Bash",
                            "input": {"command": "git commit -m test"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-07-10T10:00:02Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "[main abc1234] test",
                        }
                    ],
                },
            },
        ],
    )

    provider = ClaudeTranscriptProvider(root)
    session = provider.session_from_path(path)
    assert session is not None
    assert session.provider_id == "claudecode"
    assert session.native_session_id == session_id
    assert session.cwd == "C:/repos/work-buddy"

    turns = list(provider.iter_turns(session))
    assert [(turn.role, turn.text, turn.tools) for turn in turns] == [
        ("user", "Ship it", ()),
        ("assistant", "Running tests.", ("Bash",)),
    ]
    calls = list(provider.iter_tool_calls(session))
    assert len(calls) == 1
    assert calls[0].name == "Bash"
    assert "git commit" in calls[0].arguments_text
    assert "abc1234" in calls[0].output_text


def test_codex_provider_emits_same_canonical_contract(tmp_path):
    root = tmp_path / ".codex" / "sessions"
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    path = _write_jsonl(
        root / "2026" / "07" / "10" / "rollout-test.jsonl",
        [
            {
                "timestamp": "2026-07-10T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": "C:/repos/work-buddy",
                    "originator": "codex_cli_rs",
                    "thread_source": "user",
                },
            },
            {
                "timestamp": "2026-07-10T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Ship it"}],
                },
            },
            {
                "timestamp": "2026-07-10T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "namespace": "functions",
                    "arguments": json.dumps({"cmd": "git commit -m test"}),
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-07-10T10:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": json.dumps(
                        {"exit_code": 0, "output": "[main abc1234] test"}
                    ),
                },
            },
            {
                "timestamp": "2026-07-10T10:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Tests passed."}
                    ],
                },
            },
            # Event messages duplicate visible text in Codex rollouts and must
            # not become duplicate canonical turns.
            {
                "timestamp": "2026-07-10T10:00:04Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Tests passed."},
            },
        ],
    )

    provider = CodexTranscriptProvider(root)
    session = provider.session_from_path(path)
    assert session is not None
    assert session.provider_id == "codexcli"
    assert session.native_session_id == session_id
    assert session.project_name == "work-buddy"

    turns = list(provider.iter_turns(session))
    assert [(turn.role, turn.text, turn.tools) for turn in turns] == [
        ("user", "Ship it", ()),
        ("assistant", "", ("functions.exec_command",)),
        ("assistant", "Tests passed.", ()),
    ]
    calls = list(provider.iter_tool_calls(session))
    assert len(calls) == 1
    assert calls[0].name == "functions.exec_command"
    assert calls[0].arguments == {"cmd": "git commit -m test"}
    assert calls[0].output["exit_code"] == 0


def test_codex_provider_ignores_non_user_threads(tmp_path):
    root = tmp_path / "sessions"
    path = _write_jsonl(
        root / "rollout-subagent.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "subagent-id",
                    "thread_source": "subagent",
                },
            }
        ],
    )
    assert CodexTranscriptProvider(root).session_from_path(path) is None
