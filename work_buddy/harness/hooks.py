"""Canonical lifecycle-hook bridge for agent harnesses."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from work_buddy import paths
from work_buddy.harness.registry import get_harness


_EVENT_NAMES = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "post-tool-use": "PostToolUse",
    "stop": "Stop",
}


def handle_hook(
    event: str,
    *,
    harness_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Normalize one native lifecycle event into work-buddy behavior."""

    target = get_harness(harness_id)
    if event not in target.lifecycle_events:
        raise ValueError(f"harness {harness_id!r} does not declare hook {event!r}")
    native_event = _EVENT_NAMES[event]
    data = payload or {}
    session_id = str(data.get("session_id") or os.environ.get(target.session_env) or "")
    cwd = str(data.get("cwd") or os.getcwd())

    if not session_id:
        return _context(
            native_event,
            f"work-buddy could not determine this {target.label} session id. "
            f"Expected hook input.session_id or {target.session_env}.",
        )

    os.environ["WORK_BUDDY_SESSION_ID"] = session_id
    _record_session(target.id, session_id, data, cwd)
    if event == "session-start" and target.id == "claudecode":
        _persist_claude_session_env(session_id)

    init_context = (
        f"work-buddy session identity: {session_id}. Before using any wb_* tool, "
        f"call wb_init(session_id={json.dumps(session_id)}, "
        f"harness_id={json.dumps(target.id)})."
    )

    if event == "post-tool-use" and _recently_checked(session_id):
        return None

    context = _pending_context(
        recipient=_project_name(cwd),
        session_id=session_id,
        hook_event=native_event,
    )
    if event == "session-start":
        if context:
            return _context(native_event, f"{init_context}\n\n{context}")
        return _context(
            native_event,
            f"{init_context}\n\nwork-buddy messaging is ready; no pending messages.",
        )
    if not context:
        return None
    if event == "stop":
        return {
            "decision": "block",
            "reason": (
                "Messages need review before stopping. Read and act on them, "
                "or resolve them through work-buddy."
            ),
            "hookSpecificOutput": {
                "hookEventName": native_event,
                "additionalContext": context,
            },
        }
    return _context(native_event, context)


def parse_hook_payload(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"hook stdin is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("hook stdin must be a JSON object")
    return payload


def _record_session(
    harness_id: str,
    session_id: str,
    payload: dict[str, Any],
    cwd: str,
) -> None:
    try:
        from work_buddy.agent_session import get_session_dir, update_manifest

        get_session_dir(session_id)
        update_manifest(
            session_id=session_id,
            harness_id=harness_id,
            native_session_id=session_id,
            transcript_path=payload.get("transcript_path"),
            project=cwd,
            model=payload.get("model"),
            hook_last_seen_at=time.time(),
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
        pass


def _persist_claude_session_env(session_id: str) -> None:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    try:
        with Path(env_file).open("a", encoding="utf-8") as fh:
            fh.write(f'export WORK_BUDDY_SESSION_ID="{session_id}"\n')
    except OSError:
        pass


def _pending_context(
    *,
    recipient: str,
    session_id: str,
    hook_event: str,
) -> str:
    try:
        from work_buddy.messaging.client import _request, is_service_running

        if not is_service_running():
            return ""
        query = (
            f"/messages?recipient={quote_plus(recipient)}"
            f"&session={quote_plus(session_id)}&status=pending"
            f"&format=context&hook_event={quote_plus(hook_event)}"
        )
        result = _request("GET", query)
    except Exception:
        return ""
    if not result:
        return ""
    hook_output = result.get("hookSpecificOutput") or {}
    return str(hook_output.get("additionalContext") or "")


def _recently_checked(session_id: str, cooldown: float = 5.0) -> bool:
    root = paths.data_dir("harness/hook-rate")
    root.mkdir(parents=True, exist_ok=True)
    stamp = root / f"{_safe_filename(session_id)}.stamp"
    now = time.time()
    try:
        previous = float(stamp.read_text(encoding="ascii")) if stamp.exists() else 0.0
        if now - previous < cooldown:
            return True
        stamp.write_text(str(now), encoding="ascii")
    except (OSError, ValueError):
        return False
    return False


def _project_name(cwd: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).name
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    return Path(cwd).name or "work-buddy"


def _context(event: str, text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }


def _safe_filename(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
