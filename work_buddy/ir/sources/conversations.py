"""Conversation source adapter — harness transcripts to IR documents.

Scans enabled transcript providers, chunks each session
into 2-4 turn spans, and produces Documents with fields suitable for
fielded BM25 retrieval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.ir.sources.base import Document


class ConversationSource:
    """IR source adapter for normalized harness conversation sessions."""

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

        from work_buddy.transcripts import discover_sessions

        return [
            (str(session.path), session.mtime)
            for session in discover_sessions(
                days=days,
                project_filter=project_filter,
            )
        ]

    def parse(self, item_id: str) -> list[Document]:
        """Parse a JSONL session file into span Documents."""
        from work_buddy.transcripts import provider_for_session, session_from_path
        from work_buddy.config import load_config

        cfg = load_config()
        max_turns = cfg.get("ir", {}).get("sources", {}).get(
            "conversation", {}
        ).get("span_max_turns", 4)
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        path = Path(item_id)
        try:
            session = session_from_path(path)
        except FileNotFoundError:
            return []
        session_id = session.session_id
        project_slug = session.project_slug
        project_name = session.project_name

        # Collect all turns
        turns = [
            turn.to_dict()
            for turn in provider_for_session(session).iter_turns(session)
        ]
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

            # dense_text: first user msg + first assistant text.
            #
            # !! TOOL-ONLY SPANS ARE NOT INDEXED / NOT SEARCHABLE.
            # Spans where the first user msg AND the first assistant
            # message are both tool-call-only yield an empty dense_text
            # (user_texts[0] and asst_texts[0] are either missing or '').
            # Those spans exist in the SQLite document store but have
            # NO VECTOR in the .npz — meaning:
            #
            #   * They DO NOT participate in dense/semantic retrieval.
            #   * They DO NOT appear in ``context_search`` results for
            #     the conversation source under method='semantic' or
            #     method='keyword,semantic'.
            #   * They DO still have ``display_text`` and metadata in
            #     the SQLite store, so substring search (``method='substring'``)
            #     can still find them.
            #
            # Operational consequence: for large conversation corpora,
            # roughly HALF of all spans end up with empty dense_text.
            # Expect ``vector_count`` in ``ir_index(status)`` to be
            # ~50% of ``doc_count`` on this source. That is NOT a
            # backlog. See ``dense_eligible_docs`` / ``pending_eligible``
            # in the status payload for the real backlog signal.
            #
            # This is a deliberate retrieval-quality choice: tool-only
            # turns tend to be noise for semantic search (tool-result
            # payloads are often verbose boilerplate). But it IS a
            # limitation worth being explicit about — tool-result
            # searching is its own beast and not what the conversation
            # dense index is for.
            #
            # If it ever needs to change (e.g., tool-heavy spans turn
            # out to be retrieval-valuable for some flow), the fix is
            # to fall back to ``display_text`` here when
            # user_texts[0]+asst_texts[0] are both empty. Open design
            # question; not a bug in the current design.
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
                    "native_session_id": session.native_session_id,
                    "harness_id": session.harness_id,
                    "provider_id": session.provider_id,
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
