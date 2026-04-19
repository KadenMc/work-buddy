"""Task-note source adapter — task-linked markdown notes to IR documents.

Every task in `task_metadata.note_uuid` with a non-null UUID maps to a
markdown file at `<vault_root>/tasks/notes/<uuid>.md`. This adapter
discovers those files and emits one Document per note, enabling
hybrid (BM25 + dense) search over note BODIES via the shared IR engine.

Change detection is mtime-based (handled by the IR engine's
`indexed_items` table), so unchanged notes are skipped on rebuild.

This adapter is read-only — it never mutates the task store or the
vault. Notes whose files are missing on disk (dangling pointer) are
silently skipped in discover(), not flagged; the repair path for
dangling pointers belongs elsewhere.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from work_buddy.ir.sources.base import Document
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# First markdown H1, used as the title field when present
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


class TaskNoteSource:
    """IR source adapter for task-linked markdown notes (`tasks/notes/*.md`)."""

    @property
    def name(self) -> str:
        return "task_note"

    def default_field_weights(self) -> dict[str, float]:
        # Titles in task notes tend to restate the task line — useful anchor
        # for short-query matches. Body carries the real semantic content.
        return {"title": 2.0, "body": 1.0}

    # ------------------------------------------------------------------ discover

    def discover(self, days: int = 30) -> list[tuple[str, float]]:
        """Return `(path, mtime)` for every task-linked note that exists on disk.

        `days` is accepted for protocol compatibility but ignored — task
        notes are long-lived and cheap to check (mtime lookup). The engine's
        `indexed_items` mtime skip handles "unchanged" efficiently.
        """
        from work_buddy.config import load_config
        from work_buddy.obsidian.tasks import store as task_store
        from work_buddy.obsidian.tasks.mutations import TASK_NOTES_DIR

        cfg = load_config()
        vault_root = cfg.get("vault_root")
        if not vault_root:
            logger.warning("task_note source: vault_root not configured")
            return []

        notes_dir = Path(vault_root) / TASK_NOTES_DIR

        # Pull all non-archived tasks with a note_uuid from the store.
        # Archived tasks' notes are intentionally excluded from the index.
        conn = task_store.get_connection()
        try:
            rows = conn.execute(
                """SELECT task_id, note_uuid FROM task_metadata
                   WHERE note_uuid IS NOT NULL AND archived_at IS NULL"""
            ).fetchall()
        finally:
            conn.close()

        results: list[tuple[str, float]] = []
        missing = 0
        for row in rows:
            note_uuid = row["note_uuid"]
            note_path = notes_dir / f"{note_uuid}.md"
            try:
                stat = note_path.stat()
            except OSError:
                missing += 1
                continue
            results.append((str(note_path), stat.st_mtime))

        if missing:
            logger.info(
                "task_note discover: %d notes referenced by tasks but missing on disk",
                missing,
            )
        return results

    # ------------------------------------------------------------------ parse

    def parse(self, item_id: str) -> list[Document]:
        """Parse one `tasks/notes/<uuid>.md` file into a single Document."""
        from work_buddy.config import load_config
        from work_buddy.obsidian.tasks import store as task_store

        path = Path(item_id)
        if not path.exists():
            return []

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("task_note parse: could not read %s: %s", path, exc)
            return []

        # Strip YAML frontmatter if present
        body = raw
        if raw.startswith("---\n"):
            end = raw.find("\n---", 4)
            if end != -1:
                body = raw[end + 4 :].lstrip("\n")

        # Title: first H1 if present, else filename stem
        h1_match = _H1_RE.search(body)
        title = h1_match.group(1).strip() if h1_match else path.stem

        cfg = load_config()
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        # Map note_uuid → task_id so hits can link back to the task.
        # Schema guarantees note_uuid is unique per task (1:1 via task_create / sync),
        # but query defensively: take the first match if multiple ever appear.
        note_uuid = path.stem
        task_id: str | None = None
        task_state: str | None = None
        conn = task_store.get_connection()
        try:
            row = conn.execute(
                "SELECT task_id, state FROM task_metadata WHERE note_uuid = ? LIMIT 1",
                (note_uuid,),
            ).fetchone()
            if row:
                task_id = row["task_id"]
                task_state = row["state"]
        finally:
            conn.close()

        # dense_text: title anchors the passage; body supplies the rest.
        # embed_for_ir(role="document") will handle the passage-side encoding.
        dense_parts = [title]
        if body.strip():
            dense_parts.append(body.strip()[:max_dense])
        dense_text = "\n".join(dense_parts)[:max_dense]

        # display_text: first non-empty body line (skipping the H1), capped
        display = ""
        for line in body.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            display = stripped[:200]
            break
        if not display:
            display = title[:200]

        doc = Document(
            doc_id=f"task_note:{note_uuid}",
            source="task_note",
            fields={
                "title": title,
                "body": body[:20000],  # generous cap; dense_text already bounded
            },
            dense_text=dense_text,
            display_text=display,
            metadata={
                "note_uuid": note_uuid,
                "task_id": task_id,
                "task_state": task_state,
                "file_path": str(path),
                "indexed_at": time.time(),
            },
        )
        return [doc]
