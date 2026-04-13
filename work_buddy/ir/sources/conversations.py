"""Conversation source adapter — Claude Code JSONL sessions to IR documents.

Scans ~/.claude/projects/*/ for JSONL session files, chunks each session
into 2-4 turn spans, and produces Documents with fields suitable for
fielded BM25 retrieval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.ir.sources.base import Document


class ConversationSource:
    """IR source adapter for Claude Code conversation sessions."""

    @property
    def name(self) -> str:
        return "conversation"

    def default_field_weights(self) -> dict[str, float]:
        return {"user_text": 1.5, "assistant_text": 1.0, "tool_names": 1.75}

    def discover(self, days: int = 30) -> list[tuple[str, float]]:
        """Return (JSONL file path, mtime) for sessions modified within the window."""
        from work_buddy.config import load_config

        cfg = load_config()
        project_filter = cfg.get("chats", {}).get("project_filter", None)

        claude_dir = Path.home() / ".claude" / "projects"
        if not claude_dir.is_dir():
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        results: list[tuple[str, float]] = []

        for project_dir in sorted(claude_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            if project_filter:
                if not any(f.lower() in project_dir.name.lower() for f in project_filter):
                    continue

            for jsonl_file in project_dir.glob("*.jsonl"):
                if "subagents" in str(jsonl_file):
                    continue
                try:
                    stat = jsonl_file.stat()
                    mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    if mtime_dt < cutoff:
                        continue
                except OSError:
                    continue

                results.append((str(jsonl_file), stat.st_mtime))

        return results

    def parse(self, item_id: str) -> list[Document]:
        """Parse a JSONL session file into span Documents."""
        from work_buddy.collectors.chat_collector import iter_session_turns
        from work_buddy.config import load_config

        cfg = load_config()
        max_turns = cfg.get("ir", {}).get("sources", {}).get(
            "conversation", {}
        ).get("span_max_turns", 4)
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        path = Path(item_id)
        session_id = path.stem
        project_slug = path.parent.name

        from work_buddy.collectors.chat_collector import project_name_from_slug
        project_name = project_name_from_slug(project_slug)

        # Collect all turns
        turns = list(iter_session_turns(path))
        if not turns:
            return []

        # Chunk into spans
        docs: list[Document] = []
        i = 0
        span_idx = 0
        while i < len(turns):
            end = min(i + max_turns, len(turns))
            span = turns[i:end]

            user_texts = [t["text"][:2000] for t in span if t["role"] == "user"]
            asst_texts = [t["text"][:2000] for t in span if t["role"] == "assistant"]
            tool_names: list[str] = []
            for t in span:
                tool_names.extend(t.get("tools", []))

            # Skip empty spans (all tool results, no real content)
            if not user_texts and not asst_texts:
                i = end
                continue

            # dense_text: first user msg + first assistant text
            dense_parts = []
            if user_texts:
                dense_parts.append(user_texts[0])
            if asst_texts:
                dense_parts.append(asst_texts[0])
            dense_text = " ".join(dense_parts)[:max_dense]

            # display_text: first non-empty message for preview
            display = ""
            for _dt in user_texts + asst_texts:
                _dt_stripped = _dt.strip()
                if _dt_stripped:
                    display = _dt_stripped[:200]
                    break

            # Timestamps from span boundaries
            start_ts = span[0].get("timestamp")
            end_ts = span[-1].get("timestamp")

            doc_id = f"{session_id}:{span_idx}"
            docs.append(Document(
                doc_id=doc_id,
                source="conversation",
                fields={
                    "user_text": " ".join(user_texts),
                    "assistant_text": " ".join(asst_texts),
                    "tool_names": " ".join(sorted(set(tool_names))),
                },
                dense_text=dense_text,
                display_text=display,
                metadata={
                    "session_id": session_id,
                    "project_slug": project_slug,
                    "project_name": project_name,
                    "span_index": span_idx,
                    "start_time": start_ts,
                    "end_time": end_ts,
                },
            ))
            span_idx += 1
            i = end

        return docs
