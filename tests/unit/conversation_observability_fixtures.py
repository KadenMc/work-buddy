"""Shared fixtures for conversation-observability tests.

Synthesizes Claude Code JSONL session files in a temp directory so the
extraction helpers can be exercised without depending on the user's real
~/.claude/projects/ tree. Used by characterization tests for the old
inspector APIs and by the new conversation_observability tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# JSONL entry builders — match the live Claude Code format
# ---------------------------------------------------------------------------


def user_turn(text: str, timestamp: str, is_meta: bool = False) -> dict[str, Any]:
    """A user message turn (string content form)."""
    entry: dict[str, Any] = {
        "type": "user",
        "timestamp": timestamp,
        "message": {"content": text},
    }
    if is_meta:
        entry["isMeta"] = True
    return entry


def assistant_text(text: str, timestamp: str) -> dict[str, Any]:
    """An assistant text-only turn."""
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "content": [{"type": "text", "text": text}],
        },
    }


def assistant_bash(
    command: str,
    tool_use_id: str,
    timestamp: str,
    accompanying_text: str | None = None,
) -> dict[str, Any]:
    """An assistant turn issuing a Bash tool call."""
    content: list[dict[str, Any]] = []
    if accompanying_text:
        content.append({"type": "text", "text": accompanying_text})
    content.append({
        "type": "tool_use",
        "id": tool_use_id,
        "name": "Bash",
        "input": {"command": command},
    })
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"content": content},
    }


def assistant_write(
    tool: str,
    file_path: str,
    tool_use_id: str,
    timestamp: str,
) -> dict[str, Any]:
    """An assistant turn issuing a Write/Edit/NotebookEdit tool call.

    ``tool`` is the tool name. ``file_path`` is the target — stored
    under ``file_path`` for Write/Edit and ``notebook_path`` for
    NotebookEdit, matching the real Claude Code shape.
    """
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool,
                    "input": {key: file_path},
                }
            ],
        },
    }


def tool_result(
    tool_use_id: str,
    stdout: str,
    timestamp: str,
    is_error: bool = False,
) -> dict[str, Any]:
    """A user-side tool_result entry carrying ``toolUseResult.stdout``.

    The commit extractor reads ``entry["toolUseResult"]["stdout"]`` first
    and falls back to the in-content ``content`` field. We populate both
    paths so the fixture is robust against either reader.
    """
    return {
        "type": "user",
        "timestamp": timestamp,
        "toolUseResult": {"stdout": stdout},
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": stdout,
                    "is_error": is_error,
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# Session file writer
# ---------------------------------------------------------------------------


def write_session(
    project_dir: Path,
    session_id: str,
    entries: Iterable[dict[str, Any]],
) -> Path:
    """Write ``entries`` as a JSONL session file under ``project_dir``.

    Returns the path. Bumps the file's mtime to ``now`` so the recency
    filter in ``_recent_sessions`` keeps it within the default window.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    # Ensure mtime is "now" so default 7-day windows include the fixture.
    os.utime(path, None)
    return path


# ---------------------------------------------------------------------------
# High-level scenario builders
# ---------------------------------------------------------------------------


def commit_scenario(
    session_id: str,
    *,
    commit_hash: str = "abc1234",
    branch: str = "main",
    message: str = "Add feature X",
    files_changed: int = 3,
) -> list[dict[str, Any]]:
    """A canonical 'agent makes one commit' session.

    Three real turns precede the commit so ``message_index`` lands at
    something other than 0 — a more discriminating test than the
    degenerate single-turn case.
    """
    return [
        user_turn("First, please do X.", "2026-05-13T10:00:00Z"),
        assistant_text("Sure, here is X.", "2026-05-13T10:00:01Z"),
        user_turn("Now please commit it.", "2026-05-13T10:00:10Z"),
        assistant_bash(
            command=f"git commit -m '{message}'",
            tool_use_id="tu_1",
            timestamp="2026-05-13T10:00:11Z",
            accompanying_text="Committing now.",
        ),
        tool_result(
            tool_use_id="tu_1",
            stdout=f"[{branch} {commit_hash}] {message}\n {files_changed} files changed, 10 insertions(+)",
            timestamp="2026-05-13T10:00:12Z",
        ),
    ]


def write_scenario(
    session_id: str,
    *,
    files: list[str],
    tool: str = "Write",
    base_timestamp: str = "2026-05-13T11:00:00Z",
) -> list[dict[str, Any]]:
    """A session that writes ``files`` via the given tool, no commits."""
    entries: list[dict[str, Any]] = [
        user_turn("Please write these files.", base_timestamp),
    ]
    for i, fp in enumerate(files):
        # Bump the second-of-minute for each write so timestamp ordering
        # is unambiguous downstream.
        ts = base_timestamp.replace(":00Z", f":{i + 1:02d}Z")
        entries.append(assistant_write(tool, fp, f"tu_w{i}", ts))
    return entries
