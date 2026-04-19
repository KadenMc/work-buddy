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

from work_buddy.ir.sources.base import Document, Projection, ProjectionSpec
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
        # `line` is the authoritative task-line text (from the master list,
        # not the note's H1). `title` is the note's H1 — usually restates
        # the task but drifts if the task is renamed after the note was
        # created. `body` is the bulk of the note. BM25 over all three so
        # a keyword hit in any signal still lands.
        return {"line": 2.0, "title": 1.5, "body": 1.0}

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        # Two dense signals, mirroring the knowledge system's content+alias
        # split. They share no vector space and are RRF-fused alongside BM25:
        #
        # - ``line``  — short label-shaped canonical task text. Symmetric
        #   encoder (``leaf-mt``) because the query is also short/label-shaped.
        # - ``body``  — the note's full body. Asymmetric document encoder
        #   (``leaf-ir``) paired with ``leaf-ir-query`` at query time.
        return {
            "line": ProjectionSpec(kind="label"),
            "body": ProjectionSpec(kind="passage"),
        }

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

        # Map note_uuid → task_id + canonical task-line text so hits can
        # link back to the task AND so the ``line`` projection uses the
        # authoritative text (not the note's H1, which can drift).
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

        # Authoritative task-line text, if available, for both the ``line``
        # BM25 field and the ``line`` projection. Falls back to the note's
        # H1 if the task store or master list doesn't resolve — keeps the
        # indexer resilient to partial data.
        task_line = _lookup_task_line_text(task_id) if task_id else None
        line_text = task_line or title

        # body projection text: the note body, capped. Dense encoder
        # gets a bounded passage; BM25 can still match anywhere in the
        # fuller ``body`` field.
        body_text = body.strip()[:max_dense] if body.strip() else ""

        # display_text: first non-empty body line (skipping the H1), capped
        display = ""
        for line in body.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            display = stripped[:200]
            break
        if not display:
            display = line_text[:200]

        # dense_text is kept populated for back-compat / diagnostic reads,
        # but it's no longer the primary encoding input — projections take
        # over. Building both means no regression for any tool that reads
        # the legacy column.
        dense_text = f"{line_text}\n{body_text}"[:max_dense]

        doc = Document(
            doc_id=f"task_note:{note_uuid}",
            source="task_note",
            fields={
                "line": line_text,
                "title": title,
                "body": body[:20000],
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
            projections={
                "line": Projection(text=line_text),
                "body": Projection(text=body_text) if body_text else Projection(text=line_text),
            },
        )
        return [doc]


def _lookup_task_line_text(task_id: str) -> str | None:
    """Return the canonical task-line description for a task_id.

    Pulls from the Obsidian Tasks plugin cache when available, falling back
    to a direct scan of the master task list. Returns None if the task
    isn't resolvable — the caller should default to the note's H1.
    """
    try:
        from work_buddy.obsidian.tasks.env import verify_task
        info = verify_task(task_id=task_id)
        if info.get("found"):
            desc = (info.get("description") or "").strip()
            if desc:
                return desc
    except Exception:
        pass  # Plugin or bridge unavailable — fall through

    # Fallback: scan the master list directly
    try:
        from work_buddy.config import load_config
        from work_buddy.obsidian.tasks.mutations import (
            MASTER_TASK_FILE, _find_task_line,
        )
        vault_root = load_config().get("vault_root")
        if not vault_root:
            return None
        master = Path(vault_root) / MASTER_TASK_FILE
        if not master.exists():
            return None
        lines = master.read_text(encoding="utf-8").split("\n")
        found = _find_task_line(lines, task_id=task_id)
        if not found:
            return None
        _, line = found
        # Strip checkbox + tags + wikilinks + plugin emojis, matching the
        # cleanup that assign_task does inline.
        desc = re.sub(r"^- \[.\]\s*", "", line)
        desc = re.sub(r"#\S+", "", desc)
        desc = re.sub(r"\[\[[^\]]+\]\]", "", desc)
        desc = re.sub(r"[🆔📅✅🔼⏫]\s*\S*", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        return desc or None
    except Exception:
        return None
