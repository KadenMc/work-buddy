"""IR source over the summarization framework's durable store.

Reads `summary_items` + `summary_nodes` from `<data_root>/summarization/summarization.db`
and emits one `Document` per node. Granularity is intentional: a query like
"Kong meeting" should hit the *specific topic node* whose summary mentions
that, not just the whole session as one indivisible blob.

Each emitted Document carries:
- BM25 fields: `title` (from `extra.title`; empty for root nodes),
  `summary` (the node's primary text), `keywords` (joined from `extra.keywords`;
  weighted highest because they are explicit pointer terms).
- `dense_text`: concatenated `title summary keywords` truncated to the
  per-node cap.
- `metadata`: `namespace`, `item_id`, `level`, `ordinal`, `parent_id`,
  `source_ref` (decoded JSON if present), plus provenance (`generated_at`,
  `model`, `prompt_version`). Consumers (the retrieval funnel) use the
  metadata to map a hit back to its parent session for drill-down.

The IR engine's `item_id` is `f"{namespace}:{inner_id}"` so a single
`summary_items` row owns all its node Documents; re-summarizing an item
changes its `generated_at` (the discover mtime), and the engine clears all
prior node Documents via `delete_item_docs` before re-parsing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.ir.sources.base import Document


class SummarySource:
    """IR source adapter for the framework's per-node summary store."""

    @property
    def name(self) -> str:
        return "summary"

    def default_field_weights(self) -> dict[str, float]:
        # Keywords are highest-signal — they are explicit pointer terms the
        # LLM chose to call out. Titles are short and high-signal. Summary
        # body is the bulk of the text and gets the BM25 unit weight.
        return {"keywords": 2.0, "title": 1.75, "summary": 1.0}

    # ---------------------------------------------------------------- discover

    def discover(self, days: int = 30) -> list[tuple[str, float]]:
        """Return `(ir_item_id, mtime_seconds)` for summaries in the window.

        `ir_item_id` is `{namespace}:{inner_item_id}` — uniqueness across
        compositions sharing one DB. `mtime` is `generated_at` parsed to a
        POSIX timestamp so the engine's mtime-based change detection works
        identically to other sources.
        """
        from work_buddy.summarization.db import db_path

        path = db_path()
        if not Path(path).exists():
            return []

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT namespace, item_id, generated_at "
                "FROM summary_items "
                "WHERE generated_at >= ? AND status = 'ok' "
                "ORDER BY generated_at DESC",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()

        results: list[tuple[str, float]] = []
        for r in rows:
            ir_id = f"{r['namespace']}:{r['item_id']}"
            try:
                mtime = datetime.fromisoformat(
                    str(r["generated_at"]).replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, AttributeError):
                continue
            results.append((ir_id, mtime))
        return results

    # ---------------------------------------------------------------- parse

    def parse(self, item_id: str) -> list[Document]:
        """Parse one IR item (`namespace:inner_id`) into per-node Documents."""
        from work_buddy.config import load_config
        from work_buddy.summarization.db import db_path

        if ":" not in item_id:
            return []
        namespace, inner_id = item_id.split(":", 1)

        path = db_path()
        if not Path(path).exists():
            return []

        cfg = load_config()
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            item_row = conn.execute(
                "SELECT * FROM summary_items "
                "WHERE namespace = ? AND item_id = ?",
                (namespace, inner_id),
            ).fetchone()
            if item_row is None or item_row["status"] != "ok":
                return []
            node_rows = list(conn.execute(
                "SELECT * FROM summary_nodes "
                "WHERE namespace = ? AND item_id = ? "
                "ORDER BY ordinal",
                (namespace, inner_id),
            ))
        finally:
            conn.close()

        docs: list[Document] = []
        for row in node_rows:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except (ValueError, TypeError):
                extra = {}
            try:
                source_ref = (
                    json.loads(row["source_ref"])
                    if row["source_ref"] is not None
                    else None
                )
            except (ValueError, TypeError):
                source_ref = None

            title = str(extra.get("title", "")).strip()
            keywords = extra.get("keywords") or []
            keywords_text = (
                " ".join(str(k) for k in keywords)
                if isinstance(keywords, list)
                else ""
            )
            summary_text = row["summary"] or ""

            dense_parts: list[str] = []
            if title:
                dense_parts.append(title)
            if summary_text:
                dense_parts.append(summary_text)
            if keywords_text:
                dense_parts.append(keywords_text)
            dense_text = " ".join(dense_parts)[:max_dense]

            display = (
                f"{title}: {summary_text[:160]}".strip()
                if title
                else summary_text[:200].strip()
            )

            docs.append(Document(
                doc_id=f"{namespace}:{inner_id}:n{row['ordinal']}",
                source="summary",
                fields={
                    "title": title,
                    "summary": summary_text,
                    "keywords": keywords_text,
                },
                dense_text=dense_text,
                display_text=display,
                metadata={
                    "namespace": namespace,
                    "item_id": inner_id,
                    "level": row["level"],
                    "ordinal": row["ordinal"],
                    "parent_id": row["parent_id"],
                    "source_ref": source_ref,
                    "generated_at": item_row["generated_at"],
                    "model": item_row["model"],
                    "prompt_version": item_row["prompt_version"],
                    "extra": extra,
                },
            ))
        return docs
