"""Claude Code JSONL transcript provider."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

from work_buddy.transcripts.models import (
    TranscriptSession,
    TranscriptToolCall,
    TranscriptTurn,
)


class ClaudeTranscriptProvider:
    id = "claudecode"
    harness_id = "claudecode"
    label = "Claude Code"

    def __init__(self, projects_root: Path | None = None) -> None:
        self.projects_root = projects_root

    @property
    def root(self) -> Path:
        return self.projects_root or (Path.home() / ".claude" / "projects")

    def discover(
        self,
        *,
        days: int,
        project_filter: list[str] | None = None,
    ) -> Iterable[TranscriptSession]:
        root = self.root
        if not root.is_dir():
            return []
        cutoff = 0.0 if days <= 0 else time.time() - days * 86400
        results: list[TranscriptSession] = []
        for project_dir in sorted(root.iterdir()):
            if not project_dir.is_dir():
                continue
            if project_filter and not any(
                value.lower() in project_dir.name.lower() for value in project_filter
            ):
                continue
            for path in project_dir.glob("*.jsonl"):
                if "subagents" in path.parts:
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                session = self.session_from_path(path)
                if session is not None:
                    results.append(session)
        return results

    def session_from_path(self, path: Path) -> TranscriptSession | None:
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        if path.suffix.lower() != ".jsonl" or "subagents" in path.parts:
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        slug = path.parent.name
        return TranscriptSession(
            provider_id=self.id,
            harness_id=self.harness_id,
            session_id=path.stem,
            native_session_id=path.stem,
            path=path,
            mtime=mtime,
            project_slug=slug,
            project_name=project_name_from_slug(slug, projects_root=self.root),
            cwd=_session_cwd(path),
            originator=self.label,
        )

    def iter_turns(self, session: TranscriptSession) -> Iterable[TranscriptTurn]:
        try:
            with session.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    entry = _parse_line(line)
                    if entry is None:
                        continue
                    entry_type = entry.get("type")
                    timestamp = entry.get("timestamp")
                    content = (entry.get("message") or {}).get("content", "")
                    if entry_type == "user":
                        if entry.get("isMeta"):
                            continue
                        if isinstance(content, list):
                            if any(
                                isinstance(block, dict)
                                and block.get("type") == "tool_result"
                                for block in content
                            ):
                                continue
                            content = " ".join(
                                str(block.get("text") or "")
                                for block in content
                                if isinstance(block, dict)
                                and block.get("type") == "text"
                            )
                        if isinstance(content, str) and content.strip():
                            yield TranscriptTurn(
                                role="user",
                                text=content.strip(),
                                timestamp=timestamp,
                            )
                    elif entry_type == "assistant" and isinstance(content, list):
                        tools: list[str] = []
                        texts: list[str] = []
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                tools.append(str(block.get("name") or "unknown"))
                            elif block.get("type") == "text":
                                text = str(block.get("text") or "").strip()
                                if text:
                                    texts.append(text)
                        if texts or tools:
                            yield TranscriptTurn(
                                role="assistant",
                                text=" ".join(texts),
                                tools=tuple(tools),
                                timestamp=timestamp,
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
                    entry = _parse_line(line)
                    if entry is None:
                        continue
                    entry_type = entry.get("type")
                    content = (entry.get("message") or {}).get("content", "")
                    if entry_type == "user":
                        if entry.get("isMeta"):
                            continue
                        if isinstance(content, list):
                            results = [
                                block for block in content
                                if isinstance(block, dict)
                                and block.get("type") == "tool_result"
                            ]
                            if results:
                                for block in results:
                                    call = pending.pop(str(block.get("tool_use_id") or ""), None)
                                    if call is None:
                                        continue
                                    tool_result = entry.get("toolUseResult")
                                    stdout = (
                                        tool_result.get("stdout", "")
                                        if isinstance(tool_result, dict)
                                        else ""
                                    )
                                    call.output = stdout or block.get("content")
                                    call.is_error = bool(block.get("is_error"))
                                continue
                            text = " ".join(
                                str(block.get("text") or "")
                                for block in content
                                if isinstance(block, dict)
                                and block.get("type") == "text"
                            )
                            if text.strip():
                                turn_index += 1
                        elif isinstance(content, str) and content.strip():
                            turn_index += 1
                    elif entry_type == "assistant" and isinstance(content, list):
                        has_text = False
                        has_tools = False
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text" and str(
                                block.get("text") or ""
                            ).strip():
                                has_text = True
                            elif block.get("type") == "tool_use":
                                has_tools = True
                                call = TranscriptToolCall(
                                    call_id=str(block.get("id") or ""),
                                    name=str(block.get("name") or "unknown"),
                                    arguments=block.get("input") or {},
                                    timestamp=entry.get("timestamp"),
                                    message_index=turn_index,
                                )
                                calls.append(call)
                                pending[call.call_id] = call
                        if has_text or has_tools:
                            turn_index += 1
        except OSError:
            return []
        return calls


def project_name_from_slug(slug: str, *, projects_root: Path | None = None) -> str:
    root = projects_root or (Path.home() / ".claude" / "projects")
    if root.is_dir():
        for sibling in root.iterdir():
            if sibling.is_dir() and sibling.name != slug and slug.startswith(
                sibling.name + "-"
            ):
                return _slug_to_readable(sibling.name)
    return _slug_to_readable(slug)


def _slug_to_readable(slug: str) -> str:
    if "--" in slug:
        slug = slug.split("--", 1)[1]
    parts = slug.split("-")
    if len(parts) >= 2:
        if parts[-2].lower() in ("repos", "projects", "src", "code", "dev", "home"):
            return parts[-1]
        return f"{parts[-2]}-{parts[-1]}"
    return parts[-1] if parts else slug


def _session_cwd(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                if index >= 50:
                    break
                entry = _parse_line(line)
                if entry and entry.get("cwd"):
                    return str(entry["cwd"])
    except OSError:
        pass
    return ""


def _parse_line(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
