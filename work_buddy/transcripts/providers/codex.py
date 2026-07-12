"""Codex rollout JSONL transcript provider."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from work_buddy.transcripts.models import (
    TranscriptSession,
    TranscriptToolCall,
    TranscriptTurn,
    mtime_floor,
)


class CodexTranscriptProvider:
    id = "codexcli"
    harness_id = "codexcli"
    label = "Codex"

    def __init__(self, sessions_root: Path | None = None) -> None:
        self.sessions_root = sessions_root

    @property
    def root(self) -> Path:
        if self.sessions_root is not None:
            return self.sessions_root
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        return codex_home / "sessions"

    def discover(
        self,
        *,
        days: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        project_filter: list[str] | None = None,
    ) -> Iterable[TranscriptSession]:
        root = self.root
        if not root.is_dir():
            return []
        cutoff = mtime_floor(days, since)
        results: list[TranscriptSession] = []
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            session = self.session_from_path(path)
            if session is None:
                continue
            searchable = f"{session.cwd} {session.project_name}".lower()
            if project_filter and not any(
                value.lower() in searchable for value in project_filter
            ):
                continue
            results.append(session)
        return results

    def session_from_path(self, path: Path) -> TranscriptSession | None:
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        if path.suffix.lower() != ".jsonl":
            return None
        meta = _session_meta(path)
        if meta is None:
            return None
        thread_source = str(meta.get("thread_source") or "user")
        if thread_source not in {"user", ""}:
            return None
        native_id = str(meta.get("session_id") or meta.get("id") or "")
        if not native_id:
            return None
        cwd = str(meta.get("cwd") or "")
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        project_name = Path(cwd).name if cwd else "Codex"
        return TranscriptSession(
            provider_id=self.id,
            harness_id=self.harness_id,
            session_id=native_id,
            native_session_id=native_id,
            path=path,
            mtime=mtime,
            project_slug=cwd,
            project_name=project_name,
            cwd=cwd,
            originator=str(meta.get("originator") or self.label),
        )

    def iter_turns(self, session: TranscriptSession) -> Iterable[TranscriptTurn]:
        try:
            with session.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    record = _parse_line(line)
                    if record is None or record.get("type") != "response_item":
                        continue
                    payload = record.get("payload") or {}
                    payload_type = payload.get("type")
                    if payload_type == "message":
                        role = payload.get("role")
                        if role not in {"user", "assistant"}:
                            continue
                        text = _message_text(payload.get("content"))
                        if text:
                            yield TranscriptTurn(
                                role=str(role),
                                text=text,
                                timestamp=record.get("timestamp"),
                            )
                    elif payload_type in _TOOL_PAYLOAD_TYPES:
                        name = _tool_name(payload)
                        if name:
                            yield TranscriptTurn(
                                role="assistant",
                                text="",
                                tools=(name,),
                                timestamp=record.get("timestamp"),
                            )
        except OSError:
            return

    def iter_tool_calls(
        self,
        session: TranscriptSession,
    ) -> Iterable[TranscriptToolCall]:
        calls: list[TranscriptToolCall] = []
        pending: dict[str, TranscriptToolCall] = {}
        turn_index = 0
        try:
            with session.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    record = _parse_line(line)
                    if record is None or record.get("type") != "response_item":
                        continue
                    payload = record.get("payload") or {}
                    payload_type = payload.get("type")
                    if payload_type == "message":
                        role = payload.get("role")
                        if role in {"user", "assistant"} and _message_text(
                            payload.get("content")
                        ):
                            turn_index += 1
                    elif payload_type in _TOOL_PAYLOAD_TYPES:
                        name = _tool_name(payload)
                        if not name:
                            continue
                        call_id = str(payload.get("call_id") or payload.get("id") or "")
                        call = TranscriptToolCall(
                            call_id=call_id,
                            name=name,
                            arguments=_decode_json(payload.get("arguments")),
                            timestamp=record.get("timestamp"),
                            message_index=turn_index,
                        )
                        calls.append(call)
                        if call_id:
                            pending[call_id] = call
                        turn_index += 1
                    elif payload_type in {"function_call_output", "custom_tool_call_output"}:
                        call_id = str(payload.get("call_id") or "")
                        call = pending.pop(call_id, None)
                        if call is not None:
                            call.output = _decode_json(payload.get("output"))
        except OSError:
            return []
        return calls


_TOOL_PAYLOAD_TYPES = {
    "function_call",
    "custom_tool_call",
    "tool_search_call",
    "web_search_call",
}


def _session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                record = _parse_line(line)
                if record and record.get("type") == "session_meta":
                    payload = record.get("payload")
                    return payload if isinstance(payload, dict) else None
                if record:
                    break
    except OSError:
        return None
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts = [
        str(block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"input_text", "output_text", "text"}
        and str(block.get("text") or "").strip()
    ]
    return " ".join(texts)


def _tool_name(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or payload.get("type") or "")
    namespace = str(payload.get("namespace") or "")
    return f"{namespace}.{name}" if namespace and not name.startswith(namespace) else name


def _decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _parse_line(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
